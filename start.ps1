Set-Location -LiteralPath $PSScriptRoot
$portCandidate = if ($env:PORT) { [int]$env:PORT } else { 5000 }
while ($true) {
    $portProbe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $portCandidate)
    try { $portProbe.Start(); $portProbe.Stop(); break } catch { $portCandidate++ }
}
$env:PORT = "$portCandidate"
Write-Host "ResearchHub KZ: http://127.0.0.1:$portCandidate"
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\app.py"
