#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hosted/ auf den Root-Server bringen und den Dienst neu starten.

Aufruf (vom Projektordner, Windows oder Linux):

    python tools/deploy_hosted.py
    python tools/deploy_hosted.py --host 45.132.89.224 --key ~/.ssh/mcsm_root
    python tools/deploy_hosted.py --status        # nur nachsehen, nichts kopieren

Was passiert:

1. Der Ordner ``hosted/`` wird lokal zu einem tar.gz gepackt (ohne ``__pycache__``).
2. Das Archiv geht per ``scp`` nach ``/tmp/mcsm-deploy`` auf den Server.
3. Dort wird es entpackt und ``deploy.sh`` als root ausgefuehrt: Benutzer und Ordner anlegen,
   Dateien nach ``/opt/mcsm``, Selbsttests, systemd-Einheit installieren, Dienst neu starten.
4. Zum Schluss werden ``systemctl status mcsm`` und ``/api/health`` gezeigt.

Wiederholbar: jeder Aufruf bringt den Server auf den Stand des Projektordners. Die Daten unter
``/srv/mcsm`` bleiben unberuehrt. Es wird nur die Standardbibliothek gebraucht; ``ssh``/``scp``
kommen aus dem Betriebssystem (Windows 10/11 bringen sie mit).
"""
from __future__ import annotations

import argparse
import io
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
HOSTED = PROJECT / "hosted"

DEFAULT_HOST = "45.132.89.224"
DEFAULT_USER = "root"
DEFAULT_REMOTE = "/tmp/mcsm-deploy"
SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIX = (".pyc", ".pyo", ".tmp")
# Textdateien kommen mit Unix-Zeilenenden auf den Server: ein \r im Shell-Skript oder in der
# systemd-Einheit fuehrt sonst zu schwer zu findenden Fehlern.
TEXT_SUFFIX = (".py", ".sh", ".service", ".md", ".txt", ".json", ".conf")


def default_key() -> str:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return str(pathlib.Path(home) / ".ssh" / "mcsm_root")


def say(text: str) -> None:
    print(f"== {text}", flush=True)


def plain_output() -> None:
    """Die Windows-Konsole kann nicht jedes Zeichen aus der systemd-Ausgabe drucken."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")                 # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def build_archive(target: pathlib.Path) -> pathlib.Path:
    """hosted/ als tar.gz packen – die Dateien landen ohne fuehrenden Ordner im Archiv."""
    if not (HOSTED / "mcsmd.py").is_file():
        raise SystemExit(f"hosted/mcsmd.py fehlt unter {HOSTED} – falscher Projektordner?")
    count = 0
    with tarfile.open(target, "w:gz") as tar:
        for path in sorted(HOSTED.rglob("*")):
            rel = path.relative_to(HOSTED)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if path.is_dir():
                continue
            if path.suffix in SKIP_SUFFIX:
                continue
            info = tar.gettarinfo(str(path), arcname=str(rel).replace("\\", "/"))
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mode = 0o755 if path.suffix == ".sh" else 0o644
            if path.suffix in TEXT_SUFFIX:
                data = path.read_bytes().replace(b"\r\n", b"\n")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                with path.open("rb") as fh:
                    tar.addfile(info, fh)
            count += 1
    say(f"{count} Dateien gepackt ({target.stat().st_size // 1024} KB).")
    return target


def run(cmd: list[str], *, check: bool = True, quiet: bool = False) -> int:
    if not quiet:
        say(" ".join(cmd[:3]) + (" …" if len(cmd) > 3 else ""))
    proc = subprocess.run(cmd, check=False)
    if check and proc.returncode != 0:
        raise SystemExit(f"Abgebrochen: „{cmd[0]}“ endete mit Code {proc.returncode}.")
    return proc.returncode


def ssh_base(args) -> list[str]:
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=20"]
    if args.key:
        cmd += ["-i", args.key]
    return cmd + [f"{args.user}@{args.host}"]


def scp_base(args) -> list[str]:
    cmd = ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=20"]
    if args.key:
        cmd += ["-i", args.key]
    return cmd


def remote(args, command: str, *, check: bool = True) -> int:
    return run(ssh_base(args) + [command], check=check, quiet=True)


def capture(args, command: str) -> str:
    proc = subprocess.run(ssh_base(args) + [command], check=False,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc.stdout.decode("utf-8", errors="replace")


def show_status(args) -> None:
    say("systemctl status mcsm")
    print(capture(args, "systemctl status mcsm --no-pager || true"))
    say("API-Auskunft (/api/health)")
    print(capture(args, "curl -fsS --max-time 5 http://127.0.0.1:8765/api/health || "
                        "echo '(keine Antwort)'"))


def main(argv: list[str] | None = None) -> int:
    plain_output()
    parser = argparse.ArgumentParser(description="hosted/ auf den Root-Server ausrollen.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Ziel (Standard {DEFAULT_HOST})")
    parser.add_argument("--user", default=DEFAULT_USER, help="Anmeldename (Standard root)")
    parser.add_argument("--key", default=default_key(), help="privater SSH-Schluessel")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE, help="Ablage auf dem Server")
    parser.add_argument("--status", action="store_true", help="nur Zustand zeigen")
    parser.add_argument("--keep", action="store_true", help="Archiv auf dem Server liegen lassen")
    args = parser.parse_args(argv)

    if args.key and not pathlib.Path(args.key).is_file():
        say(f"Hinweis: {args.key} gibt es nicht – es wird ohne -i versucht.")
        args.key = ""

    if args.status:
        show_status(args)
        return 0

    started = time.time()
    with tempfile.TemporaryDirectory(prefix="mcsm-deploy-") as tmp:
        archive = build_archive(pathlib.Path(tmp) / "hosted.tar.gz")
        say(f"Verbindung zu {args.user}@{args.host} wird geprueft.")
        remote(args, "test -d /srv || exit 1")
        remote(args, f"rm -rf {args.remote_dir} && mkdir -p {args.remote_dir}")
        say(f"Archiv wird nach {args.remote_dir} kopiert.")
        run(scp_base(args) + [str(archive), f"{args.user}@{args.host}:{args.remote_dir}/hosted.tar.gz"],
            quiet=True)

    say("deploy.sh wird auf dem Server ausgefuehrt.")
    code = remote(args, f"cd {args.remote_dir} && tar xzf hosted.tar.gz && sh deploy.sh",
                  check=False)
    if not args.keep:
        remote(args, f"rm -rf {args.remote_dir}", check=False)
    if code != 0:
        say("Das Ausrollen ist gescheitert – die Meldungen stehen oben.")
        print(capture(args, "journalctl -u mcsm -n 40 --no-pager || true"))
        return code
    show_status(args)
    say(f"Fertig in {int(time.time() - started)} s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
