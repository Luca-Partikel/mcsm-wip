#!/usr/bin/env python3
"""Baut das Begleit-Plugin MCSMCompanion (nur Standardbibliothek, javac + jar).

Aufruf:  python tools/build_plugin.py [servers/<id>]

Der angegebene (oder automatisch gefundene) Paper-Serverordner liefert den Classpath:
versions/<v>/paper-<v>.jar und libraries/**/*.jar entstehen beim ersten Start eines
Paper-Servers. Ergebnis: assets/MCSMCompanion.jar mit plugin.yml im Jar-Wurzelverzeichnis.
"""
from __future__ import annotations

import glob
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "plugin" / "src" / "main" / "java"
RES = ROOT / "plugin" / "src" / "main" / "resources"
CLASSES = ROOT / "build" / "plugin" / "classes"
API_COPY = ROOT / "build" / "plugin" / "api"
OUT = ROOT / "assets" / "MCSMCompanion.jar"
RELEASE = "21"
MAX_CLASS_VERSION = 65          # Klassendatei-Version von Java 21
CLASS_MAGIC = bytes.fromhex("cafebabe")


def fail(msg: str) -> "NoReturn":  # noqa: F821
    print(f"FEHLER: {msg}", file=sys.stderr)
    sys.exit(1)


def find_jdk_tool(name: str) -> pathlib.Path:
    exe = name + (".exe" if os.name == "nt" else "")
    hits = sorted(glob.glob(str(ROOT / "runtime" / "jdk21" / "*" / "bin" / exe)))
    if not hits:
        fail(f"{exe} nicht gefunden. Erwartet unter runtime/jdk21/<jdk>/bin/. "
             "Bitte ein JDK 21 dort entpacken (z. B. Temurin jdk-21.x).")
    return pathlib.Path(hits[-1])


def find_server(arg: str | None) -> pathlib.Path:
    if arg:
        sdir = pathlib.Path(arg)
        if not sdir.is_absolute():
            sdir = (ROOT / sdir).resolve()
        if not sdir.is_dir():
            fail(f"Serverordner nicht gefunden: {sdir}")
        return sdir
    candidates = []
    for sdir in sorted((ROOT / "servers").glob("*")):
        if sdir.is_dir() and glob.glob(str(sdir / "versions" / "*" / "paper-*.jar")):
            candidates.append(sdir)
    if not candidates:
        fail("Kein Paper-Server mit entpacktem versions/<v>/paper-<v>.jar unter servers/ gefunden. "
             "Bitte einen Java-Server einmal starten oder den Ordner als Argument angeben.")
    return candidates[0]


def build_classpath(sdir: pathlib.Path) -> list[pathlib.Path]:
    papers = sorted(pathlib.Path(p) for p in glob.glob(str(sdir / "versions" / "*" / "paper-*.jar")))
    if not papers:
        fail(f"In {sdir} liegt kein versions/<v>/paper-<v>.jar. Der Server muss einmal gestartet worden sein.")
    libs = sorted(pathlib.Path(p) for p in glob.glob(str(sdir / "libraries" / "**" / "*.jar"), recursive=True))
    if not libs:
        fail(f"In {sdir} fehlt der Ordner libraries/ mit den Paper-Bibliotheken.")
    if not any("paper-api" in p.name for p in libs):
        print("WARNUNG: libraries/ enthält kein paper-api-*.jar – Compile könnte fehlschlagen.")
    return [papers[-1], *libs]


def class_major(data: bytes) -> int | None:
    if len(data) >= 8 and data[:4] == CLASS_MAGIC:
        return struct.unpack(">H", data[6:8])[0]
    return None


def compile_time_copy(jar: pathlib.Path) -> pathlib.Path:
    """Paper 26.x ist mit Java 25 gebaut (Klassendatei-Version 69). javac 21 liest solche Jars
    nicht, obwohl es nur die Signaturen braucht. Deshalb wird für den Compiler eine Kopie unter
    build/plugin/api/ angelegt, in der die Versionsnummer der Klassendateien auf 65 (Java 21)
    gesetzt ist. Das Plugin selbst wird unverändert für --release 21 übersetzt; die Kopie landet
    nie im Ergebnis. Jars, die ohnehin lesbar sind, werden unverändert benutzt."""
    needs_patch = False
    with zipfile.ZipFile(jar) as zf:
        for info in zf.infolist():
            if info.filename.endswith(".class"):
                major = class_major(zf.read(info)[:8])
                if major is not None and major > MAX_CLASS_VERSION:
                    needs_patch = True
                    break
    if not needs_patch:
        return jar
    API_COPY.mkdir(parents=True, exist_ok=True)
    target = API_COPY / jar.name
    if target.is_file() and target.stat().st_size > 0 and target.stat().st_mtime >= jar.stat().st_mtime:
        return target
    with zipfile.ZipFile(jar) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith(".class"):
                major = class_major(data)
                if major is not None and major > MAX_CLASS_VERSION:
                    data = data[:4] + struct.pack(">HH", 0, MAX_CLASS_VERSION) + data[8:]
            dst.writestr(info.filename, data)
    return target


def run(cmd: list[str]) -> int:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.stdout.strip():
        print(proc.stdout)
    if proc.stderr.strip():
        print(proc.stderr, file=sys.stderr)
    return proc.returncode


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    javac = find_jdk_tool("javac")
    jar = find_jdk_tool("jar")
    sdir = find_server(arg)
    classpath = build_classpath(sdir)

    sources = sorted(SRC.rglob("*.java"))
    if not sources:
        fail(f"Keine Quelldateien unter {SRC}")
    plugin_yml = RES / "plugin.yml"
    if not plugin_yml.is_file():
        fail(f"{plugin_yml} fehlt")

    print(f"JDK      : {javac.parent.parent}")
    print(f"Server   : {sdir}")
    print(f"Classpath: {len(classpath)} Jars (Paper {classpath[0].name})")
    print(f"Quellen  : {len(sources)} Dateien")

    compile_cp = [compile_time_copy(p) for p in classpath]
    patched = sum(1 for a, b in zip(classpath, compile_cp) if a != b)
    if patched:
        print(f"Hinweis  : {patched} Jar(s) mit Klassendatei-Version > {MAX_CLASS_VERSION} für javac {RELEASE} "
              f"nach {API_COPY.relative_to(ROOT)} kopiert (nur Signaturen, nicht Teil des Ergebnisses).")

    if CLASSES.exists():
        shutil.rmtree(CLASSES)
    CLASSES.mkdir(parents=True)

    # Argumentdatei, damit lange Classpaths die Windows-Kommandozeilenlänge nicht sprengen.
    argfile = CLASSES.parent / "javac.args"
    sep = ";" if os.name == "nt" else ":"
    with argfile.open("w", encoding="utf-8") as fh:
        fh.write(f"--release {RELEASE}\n")
        fh.write("-encoding UTF-8\n")
        fh.write("-proc:none\n")
        fh.write("-Xlint:all,-serial,-classfile\n")
        fh.write("-Werror\n")
        fh.write(f'-d "{CLASSES.as_posix()}"\n')
        fh.write(f'-cp "{sep.join(p.as_posix() for p in compile_cp)}"\n')
        for s in sources:
            fh.write(f'"{s.as_posix()}"\n')

    print("javac …")
    if run([str(javac), f"@{argfile}"]) != 0:
        fail("javac ist fehlgeschlagen (siehe Meldungen oben).")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    print("jar …")
    code = run([str(jar), "--create", "--file", str(OUT), "-C", str(CLASSES), ".", "-C", str(RES), "."])
    if code != 0 or not OUT.is_file():
        fail("jar ist fehlgeschlagen.")

    with zipfile.ZipFile(OUT) as zf:
        names = zf.namelist()
    classes = [n for n in names if n.endswith(".class")]
    if "plugin.yml" not in names:
        fail("plugin.yml fehlt im Jar-Wurzelverzeichnis.")
    if not classes:
        fail("Keine .class-Dateien im Jar.")
    print(f"OK: {OUT} ({OUT.stat().st_size // 1024} KB, {len(classes)} Klassen, plugin.yml vorhanden)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
