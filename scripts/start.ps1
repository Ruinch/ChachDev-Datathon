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
    [switch]$RequireAnalyzer,
    [switch]$AllowMissingMediaTools
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
Import-ProjectEnvironment -Path (Join-Path $projectRoot '.env')
$pythonPath = Get-ProjectPython -ProjectRoot $projectRoot
& (Join-Path $PSScriptRoot 'check.ps1') -RequireAnalyzer:$RequireAnalyzer -AllowMissingMediaTools:$AllowMissingMediaTools

Push-Location -LiteralPath $projectRoot
try {
    Write-Host "Starting the local interface at http://${BindAddress}:${Port}/. Stop with Ctrl+C."
    & $pythonPath -m uvicorn app.main:app --host $BindAddress --port $Port --workers 1
    if ($LASTEXITCODE -ne 0) { throw 'The server exited with an error. Review the server log above.' }
}
finally { Pop-Location }
