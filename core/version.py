"""Versionsnummer und Update-Quelle des Minecraft Server Managers.

`tools\\release.py 1.6.0` hebt die Version an, committet und taggt; die GitHub-Action baut daraus das
Release (ZIP, Setup.exe, SHA256SUMS.txt). Der Manager vergleicht diese Version mit dem neuesten Release.
"""
__version__ = "1.8.1"

# GitHub-Repository "besitzer/repo", aus dem Updates geladen werden. Leer = Update-Prüfung aus.
# Zum Testen überschreibbar per Umgebungsvariable MCSM_UPDATE_REPO bzw. MCSM_UPDATE_API (volle URL).
UPDATE_REPO = "Luca-Partikel/mcsm-wip"
