#!/usr/bin/env python3
"""Baut die Weitergabe-Pakete des Minecraft Server Managers:

  dist/MinecraftServerManager.zip         – ZIP mit Install.bat (entpacken, Install.bat doppelklicken)
  dist/MinecraftServerManager-Setup.exe   – Setup-Datei (IExpress, Windows-Bordmittel): entpackt das ZIP
                                            und ruft Install.bat auf

Aufruf:  python tools\\build_installer.py     (oder tools\\Build-Installer.bat)
Nur Standardbibliothek; IExpress liegt unter C:\\Windows\\System32\\iexpress.exe.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
ZIP_NAME = "MinecraftServerManager.zip"
SETUP_NAME = "MinecraftServerManager-Setup.exe"

# Dateien, die zum Programm gehören (keine Downloads, keine Welten, keine Logs).
INCLUDE = [
    "app.py", "Start.bat", "Start-Debug.bat", "Start.vbs", "Install.bat", "README.md", "app.ico",
    "core/__init__.py", "core/store.py", "core/sources.py", "core/manager.py", "core/tray.py", "core/version.py", "core/updater.py", "core/companion.py", "core/modpacks.py", "core/players.py", "core/cloud.py", "assets/MCSMCompanion.jar",
    "web/index.html", "web/style.css", "web/app.js", "web/logo.svg", "web/favicon.png",
    "web/icon-192.png", "web/icon-512.png", "web/manifest.webmanifest", "tools/make_icons.py",
    "tools/shortcuts.ps1", "tools/stop-instance.ps1", "tools/build_installer.py", "tools/Build-Installer.bat",
]


# ------------------------------------------------------------------ Icon (ohne Bibliotheken)

def _make_icon(path: pathlib.Path, size: int = 48) -> None:
    """Erzeugt ein einfaches Würfel-Icon als 32-Bit-ICO (BMP-Format, mit Alphakanal)."""
    green, dark, light, edge = (0x3D, 0xDC, 0x84), (0x1E, 0x8E, 0x54), (0x8A, 0xF0, 0xB8), (0x12, 0x5A, 0x36)
    px = [[(0, 0, 0, 0)] * size for _ in range(size)]
    m = size // 8                      # Rand
    top = size // 2 - m                # Höhe der „Oberseite“
    for y in range(m, size - m):
        for x in range(m, size - m):
            # Rundung der Ecken
            cx = min(x - m, size - m - 1 - x)
            cy = min(y - m, size - m - 1 - y)
            if cx + cy < m // 2:
                continue
            if y < top:
                col = light                       # Oberseite
            elif x < size // 2:
                col = green                       # linke Vorderseite
            else:
                col = dark                        # rechte Seite (Schatten)
            if abs(x - size // 2) <= 1 and y >= top:
                col = edge                        # senkrechte Kante
            if abs(y - top) <= 1:
                col = edge                        # waagerechte Kante
            px[y][x] = (*col, 255)

    # BITMAPINFOHEADER + XOR-Bitmap (BGRA, von unten nach oben) + AND-Maske
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    xor = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            r, g, b, a = px[y][x]
            xor += bytes((b, g, r, a))
    row_bytes = ((size + 31) // 32) * 4
    and_mask = bytes(row_bytes * size)
    image = header + bytes(xor) + and_mask
    ico = struct.pack("<HHH", 0, 1, 1)
    ico += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), 6 + 16)
    path.write_bytes(ico + image)


# ------------------------------------------------------------------ ZIP

def build_zip() -> pathlib.Path:
    DIST.mkdir(exist_ok=True)
    target = DIST / ZIP_NAME
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in INCLUDE:
            src = ROOT / rel
            if not src.exists():
                print(f"  (übersprungen, fehlt: {rel})")
                continue
            zf.write(src, rel)
    return target


# ------------------------------------------------------------------ Setup.exe (IExpress)

def build_setup(zip_path: pathlib.Path) -> pathlib.Path | None:
    iexpress = pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "iexpress.exe"
    if sys.platform != "win32" or not iexpress.exists():
        print("  IExpress nicht gefunden – nur das ZIP wurde erstellt.")
        return None
    work = DIST / "sfx"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    shutil.copy2(zip_path, work / ZIP_NAME)
    shutil.copy2(ROOT / "Install.bat", work / "Install.bat")
    setup = DIST / SETUP_NAME
    setup.unlink(missing_ok=True)

    sed = work / "setup.sed"
    sed.write_text(
        "[Version]\nClass=IEXPRESS\nSEDVersion=3\n"
        "[Options]\nPackagePurpose=InstallApp\nShowInstallProgramWindow=1\nHideExtractAnimation=0\n"
        "UseLongFileName=1\nInsideCompressed=0\nCAB_FixedSize=0\nCAB_ResvCodeSigning=0\nRebootMode=N\n"
        "InstallPrompt=%InstallPrompt%\nDisplayLicense=%DisplayLicense%\nFinishMessage=%FinishMessage%\n"
        "TargetName=%TargetName%\nFriendlyName=%FriendlyName%\nAppLaunched=%AppLaunched%\n"
        "PostInstallCmd=%PostInstallCmd%\nAdminQuietInstCmd=%AdminQuietInstCmd%\nUserQuietInstCmd=%UserQuietInstCmd%\n"
        "SourceFiles=SourceFiles\n"
        "[Strings]\n"
        "InstallPrompt=Minecraft Server Manager jetzt installieren?\n"
        "DisplayLicense=\n"
        "FinishMessage=\n"
        f"TargetName={setup}\n"
        "FriendlyName=Minecraft Server Manager - Setup\n"
        "AppLaunched=cmd.exe /c Install.bat\n"
        "PostInstallCmd=<None>\n"
        "AdminQuietInstCmd=\n"
        "UserQuietInstCmd=\n"
        f'FILE0="{ZIP_NAME}"\n'
        'FILE1="Install.bat"\n'
        "[SourceFiles]\n"
        f"SourceFiles0={work}\\\n"
        "[SourceFiles0]\n"
        "%FILE0%=\n"
        "%FILE1%=\n",
        encoding="cp1252",
    )
    # IExpress entfernt die Anführungszeichen nicht, die subprocess bei Leerzeichen im Pfad setzt
    # (rc=1 ohne Ausgabe) – deshalb nur der Dateiname, Arbeitsverzeichnis ist der SED-Ordner.
    res = subprocess.run([str(iexpress), "/N", "/Q", sed.name], cwd=str(work), capture_output=True, text=True)
    if not setup.exists():
        print("  IExpress konnte die Setup.exe nicht erstellen:", res.stdout, res.stderr)
        return None
    shutil.rmtree(work, ignore_errors=True)
    return setup


def main() -> int:
    print("Logo und Icons werden erzeugt …")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import make_icons                                  # noqa: E402 – liegt im selben Ordner
    make_icons.make_all(verbose=False)
    print("ZIP wird gebaut …")
    zip_path = build_zip()
    print(f"  {zip_path} ({zip_path.stat().st_size / 1024:.0f} KB)")
    print("Setup.exe wird gebaut (IExpress) …")
    setup = build_setup(zip_path)
    if setup:
        print(f"  {setup} ({setup.stat().st_size / 1024:.0f} KB)")
    print("\nWeitergeben: dist\\" + (SETUP_NAME if setup else ZIP_NAME))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
