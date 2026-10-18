<# Loads only the dedicated jury profile. Does not configure or open Funnel. #>
[CmdletBinding()]
param(
    [string]$ProfilePath,
    [switch]$CheckOnly,
    [switch]$InterfaceOnly,
    [switch]$StartModel,
    [switch]$UseExistingModel
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$ProfilePath = Resolve-LaptopProfilePath -ProjectRoot $projectRoot -ProfilePath $ProfilePath
$manifest = Get-Content -LiteralPath (Join-Path $ProfilePath 'profile.json') -Raw | ConvertFrom-Json
$envPath = Join-Path $ProfilePath 'service.env'
if ($manifest.version -ne 1 -or $manifest.purpose -ne 'jury' -or $manifest.env_path -ne $envPath) {
    throw 'Invalid jury profile. Prepare a dedicated profile before starting.'
}
# A personal APP_DATA_DIR or key in the caller's terminal must never override this profile.
$previous = @{}
Get-ChildItem Env: | Where-Object { $_.Name -like 'APP_*' -or $_.Name -in @('HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE') } | ForEach-Object {
    $previous[$_.Name] = $_.Value
    [Environment]::SetEnvironmentVariable($_.Name, $null, 'Process')
}
try {
    $null = Initialize-ProjectEnvironment -ProjectRoot $projectRoot -EnvPath $envPath
    if ($env:APP_DATA_DIR -ne $manifest.data_dir -or $env:APP_RULE_CATALOG_PATH -ne $manifest.rules_path) {
        throw 'Jury storage/catalog differs from its profile manifest. Refusing mixed data.'
    }
    if ($env:APP_LOCAL_MODEL_URL -ne 'http://127.0.0.1:8091' -or $env:APP_ANALYZER_FACTORY -ne 'app.local_analyzer:build_analyzer') {
        throw 'This launcher requires the pinned local llama.cpp adapter on loopback port 8091.'
    }
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    if ($CheckOnly) {
        if ($StartModel) {
            & (Join-Path $PSScriptRoot 'start-ml.ps1') -EnvPath $envPath -BindAddress '127.0.0.1' -Port 8000 -ModelPort 8091 -RequireAnalyzer:(!$InterfaceOnly) -CheckOnly
        } else {
            & (Join-Path $PSScriptRoot 'check.ps1') -EnvPath $envPath -RequireAnalyzer:(!$InterfaceOnly)
        }
    } elseif ($InterfaceOnly -and -not $StartModel) {
        & (Join-Path $PSScriptRoot 'start.ps1') -EnvPath $envPath -BindAddress '127.0.0.1' -Port 8000
    } else {
        & (Join-Path $PSScriptRoot 'start-ml.ps1') -EnvPath $envPath -BindAddress '127.0.0.1' -Port 8000 -ModelPort 8091 -RequireAnalyzer:(!$InterfaceOnly) -UseExistingModel:$UseExistingModel
    }
} finally {
    Get-ChildItem Env: | Where-Object { $_.Name -like 'APP_*' -or $_.Name -in @('HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE') } | ForEach-Object {
        [Environment]::SetEnvironmentVariable($_.Name, $null, 'Process')
    }
    foreach ($name in $previous.Keys) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
}
