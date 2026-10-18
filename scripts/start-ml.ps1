<# Starts the experimental local model and the existing service. Downloads nothing. #>
[CmdletBinding()]
param(
    [string]$BindAddress = '127.0.0.1',
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [ValidateRange(1, 65535)][int]$ModelPort = 8091,
    [string]$EnvPath
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$null = Initialize-ProjectEnvironment -ProjectRoot $projectRoot -EnvPath $EnvPath
if (-not $env:APP_ANALYZER_FACTORY) { $env:APP_ANALYZER_FACTORY = 'app.local_analyzer:build_analyzer' }
if (-not $env:APP_RULE_CATALOG_PATH) { $env:APP_RULE_CATALOG_PATH = Join-Path $projectRoot 'app/data/ml-rules.json' }
if (-not $env:APP_ML_INPUT_MODE) { $env:APP_ML_INPUT_MODE = 'video' }
if (-not $env:APP_ML_FRAMES) {
    $env:APP_ML_FRAMES = $(if ($env:APP_ML_INPUT_MODE -eq 'video') { '24' } else { '12' })
}
$endpoint = "http://127.0.0.1:$ModelPort"
if ($env:APP_LOCAL_MODEL_URL -and $env:APP_LOCAL_MODEL_URL -ne $endpoint) {
    throw 'APP_LOCAL_MODEL_URL differs from ModelPort. Use the matching local port or start the model separately.'
}
$env:APP_LOCAL_MODEL_URL = $endpoint

function Test-LocalModel {
    try {
        $request = [Net.HttpWebRequest]::Create("$endpoint/health")
        $request.Proxy = $null
        $request.AllowAutoRedirect = $false
        $request.Timeout = 1500
        $response = $request.GetResponse()
        try { return [int]$response.StatusCode -eq 200 } finally { $response.Dispose() }
    } catch { return $false }
}

$ownedModel = $null
try {
    & (Join-Path $PSScriptRoot 'check.ps1') -EnvPath $EnvPath -RequireAnalyzer
    if (-not (Test-LocalModel)) {
        $ownedModel = & (Join-Path $projectRoot 'ml_tools/start_model.ps1') -Model '3.5-4b' -Port $ModelPort -Background
        $deadline = [DateTime]::UtcNow.AddSeconds(90)
        while (-not (Test-LocalModel)) {
            if ($ownedModel.HasExited) { throw 'Local model exited during startup. Check CUDA, weights and GPU memory.' }
            if ([DateTime]::UtcNow -gt $deadline) { throw 'Local model did not become ready within 90 seconds.' }
            Start-Sleep -Milliseconds 500
        }
    }
    Write-Host 'Local model ready. Experimental procedure scope; see docs/ml-measurements.md for measured limitations.'
    & (Join-Path $PSScriptRoot 'start.ps1') -BindAddress $BindAddress -Port $Port -EnvPath $EnvPath -RequireAnalyzer
} finally {
    if ($ownedModel -and -not $ownedModel.HasExited) { Stop-Process -Id $ownedModel.Id }
}
