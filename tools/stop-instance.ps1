# Beendet einen laufenden Minecraft Server Manager im Zielordner (vor einem Update),
# damit nach der Installation nicht der alte Code weiterlaeuft. Server werden dabei
# ueber /api/shutdown sauber gestoppt (Welten gespeichert).
param([Parameter(Mandatory = $true)][string]$Target)

$file = Join-Path $Target 'data\instance.json'
if (-not (Test-Path $file)) { exit 0 }

try { $inst = Get-Content $file -Raw | ConvertFrom-Json } catch { exit 0 }
$proc = $null
try { $proc = Get-Process -Id $inst.pid -ErrorAction Stop } catch { }
if ($null -eq $proc) { Remove-Item $file -ErrorAction SilentlyContinue; exit 0 }

Write-Host "  Laufender Manager wird beendet (Server werden gestoppt) ..."
try {
    Invoke-RestMethod -Method Post -Uri ("http://127.0.0.1:{0}/api/shutdown" -f $inst.port) `
        -Headers @{ 'X-Token' = $inst.token } -TimeoutSec 5 | Out-Null
} catch { }
for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Milliseconds 500
    if (-not (Get-Process -Id $inst.pid -ErrorAction SilentlyContinue)) { break }
}
if (Get-Process -Id $inst.pid -ErrorAction SilentlyContinue) {
    Write-Host "  Manager reagiert nicht - wird beendet."
    Stop-Process -Id $inst.pid -Force -ErrorAction SilentlyContinue
    Start-Sleep 1
}
Remove-Item $file -ErrorAction SilentlyContinue
