' Startet den Minecraft Server Manager ohne sichtbares Konsolenfenster.
' (Ziel der Desktop-/Startmenue-Verknuepfung.)
Option Explicit
Dim sh, fso, dir
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run "cmd.exe /c """ & dir & "\Start.bat"" /hidden", 0, False
