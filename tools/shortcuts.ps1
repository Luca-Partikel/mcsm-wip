# Legt Verknuepfungen fuer den Minecraft Server Manager an (Desktop + Startmenue).
param([Parameter(Mandatory = $true)][string]$Target)

$ErrorActionPreference = 'Stop'
$vbs = Join-Path $Target 'Start.vbs'
$icon = Join-Path $Target 'app.ico'
$shell = New-Object -ComObject WScript.Shell

$places = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
)
foreach ($dir in $places) {
    if (-not (Test-Path $dir)) { continue }
    $lnk = $shell.CreateShortcut((Join-Path $dir 'Minecraft Server Manager.lnk'))
    $lnk.TargetPath = 'wscript.exe'
    $lnk.Arguments = '//B "' + $vbs + '"'
    $lnk.WorkingDirectory = $Target
    $lnk.Description = 'Minecraft Server Manager - Bedrock & Java Server lokal einrichten'
    if (Test-Path $icon) { $lnk.IconLocation = $icon + ',0' }
    $lnk.Save()
    Write-Host ("  Verknuepfung: " + (Join-Path $dir 'Minecraft Server Manager.lnk'))
}
