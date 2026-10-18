<# Local annotation only. Never expose this port through Funnel. #>
[CmdletBinding()]
param(
    [string]$QueuePath,
    [ValidateRange(1024, 65535)][int]$Port = 8767
)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
if (-not $QueuePath) {
    $QueuePath = Join-Path $projectPath '.local_ml\quality-v17-error-review\queue.json'
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'The project Python environment is missing. Prepare .venv first.'
}
if (-not (Test-Path -LiteralPath $QueuePath -PathType Leaf)) {
    throw 'The private annotation queue is missing on this computer. It is intentionally not published in Git.'
}
$QueuePath = (Resolve-Path -LiteralPath $QueuePath).Path
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Keep the existing reviewer open; no process was stopped."
}
Push-Location -LiteralPath $projectPath
try {
    Write-Host "Local annotation: http://127.0.0.1:$Port/ . Keep this terminal open."
    & $pythonPath -m ml_tools.review_server --queue $QueuePath --port $Port --save
    if ($LASTEXITCODE -ne 0) { throw 'Local reviewer stopped with an error.' }
} finally {
    Pop-Location
}
