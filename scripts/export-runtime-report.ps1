<# Saves a private, sanitized environment fingerprint. Downloads nothing. #>
[CmdletBinding()]
param([string]$EnvPath, [string]$OutputPath, [switch]$VerifyModel)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$null = Initialize-ProjectEnvironment -ProjectRoot $projectRoot -EnvPath $EnvPath
$pythonPath = Get-ProjectPython -ProjectRoot $projectRoot
if (-not $OutputPath) { $OutputPath = '.local_data/scratch/runtime-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.json' }
$reportPath = Resolve-ProjectPath -ProjectRoot $projectRoot -Path $OutputPath
$arguments = @('-m', 'ml_tools.runtime_report', '--output', $reportPath)
if ($VerifyModel) { $arguments += '--verify-model' }
Push-Location -LiteralPath $projectRoot
try {
    & $pythonPath @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Environment report could not be created. Existing reports and configuration were preserved.' }
}
finally { Pop-Location }
