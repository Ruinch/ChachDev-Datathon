<#
.SYNOPSIS
Starts the local API with one worker using the project virtual environment.
.EXAMPLE
.\scripts\start.ps1
.EXAMPLE
.\scripts\start.ps1 -AllowMissingMediaTools
#>
[CmdletBinding()]
param(
    [string]$BindAddress = '127.0.0.1',
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [string]$EnvPath,
    [switch]$RequireAnalyzer,
    [switch]$AllowMissingMediaTools
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$null = Initialize-ProjectEnvironment -ProjectRoot $projectRoot -EnvPath $EnvPath
if ([string]::IsNullOrWhiteSpace($env:APP_API_KEY)) {
    throw 'APP_API_KEY is missing. Run scripts/setup.ps1 first, or set a key in your existing .env.'
}
$pythonPath = Get-ProjectPython -ProjectRoot $projectRoot
& (Join-Path $PSScriptRoot 'check.ps1') -EnvPath $EnvPath -RequireAnalyzer:$RequireAnalyzer -AllowMissingMediaTools:$AllowMissingMediaTools

Push-Location -LiteralPath $projectRoot
try {
    Write-Host "Starting the local interface at http://${BindAddress}:${Port}/. Stop with Ctrl+C."
    & $pythonPath -m uvicorn app.main:app --host $BindAddress --port $Port --workers 1
    if ($LASTEXITCODE -ne 0) { throw 'The server exited with an error. Review the server log above.' }
}
finally { Pop-Location }
