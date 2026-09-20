#!/usr/bin/env python3
"""Neue Version veröffentlichen.

  python tools\\release.py 1.6.0            Version setzen, Paket probeweise bauen, git commit + tag v1.6.0
  python tools\\release.py 1.6.0 --push     … und zusätzlich zu GitHub pushen (löst die Release-Action aus)

Die GitHub-Action (.github/workflows/release.yml) baut beim Tag-Push automatisch
MinecraftServerManager.zip, MinecraftServerManager-Setup.exe und SHA256SUMS.txt und legt das Release an.
Installierte Manager sehen das Update danach unten links in der Oberfläche.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "core" / "version.py"


def run(*cmd: str, check: bool = True) -> str:
    res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if check and res.returncode != 0:
        raise SystemExit(f"Fehler bei: {' '.join(cmd)}\n{res.stdout}{res.stderr}")
    return (res.stdout or "").strip()


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not re.fullmatch(r"\d+\.\d+\.\d+", argv[1]):
        print(__doc__)
        return 1
    version = argv[1]
    push = "--push" in argv

    text = VERSION_FILE.read_text(encoding="utf-8")
    current = re.search(r'__version__ = "([^"]+)"', text).group(1)
    if tuple(map(int, version.split("."))) <= tuple(map(int, current.split("."))):
        raise SystemExit(f"Neue Version {version} muss größer sein als die aktuelle {current}.")
    if "UPDATE_REPO = \"\"" in text:
        print("WARNUNG: UPDATE_REPO in core/version.py ist leer – installierte Manager finden das Update nicht.")

    print(f"Version {current} → {version}")
    VERSION_FILE.write_text(text.replace(f'__version__ = "{current}"', f'__version__ = "{version}"'), encoding="utf-8")

    print("Probebau der Pakete …")
    run(sys.executable, str(ROOT / "tools" / "build_installer.py"))

    if not (ROOT / ".git").exists():
        run("git", "init")
    run("git", "add", "-A")
    run("git", "commit", "-m", f"Release v{version}", check=False)
    run("git", "tag", "-a", f"v{version}", "-m", f"Version {version}")
    print(f"Commit und Tag v{version} angelegt.")

    if push:
        print("Push zu GitHub …")
        run("git", "push", "origin", "HEAD")
        run("git", "push", "origin", f"v{version}")
        print("Fertig – die GitHub-Action baut jetzt das Release (Actions-Tab im Repository).")
    else:
        print("Noch nicht gepusht. Zum Veröffentlichen:\n  git push origin HEAD && git push origin v" + version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
