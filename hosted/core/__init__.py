"""Bausteine des Hosted-Daemons (Root-Server).

Die Module hier nutzen untereinander **relative** Importe (``from . import store_hosted``),
damit sie unverändert als Paket in ``/opt/mcsm/core/`` liegen können. Diese Datei macht den
Ordner zu einem Paket; sie tut absichtlich nichts weiter, damit ``mcsmd.py`` und die
Selbsttests das Paket auch unter einem eigenen Namen laden können (im Projektordner des
PC-Programms liegt ein gleichnamiger Ordner ``core/``).
"""
