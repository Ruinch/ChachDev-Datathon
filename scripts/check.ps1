<#
.SYNOPSIS
Checks the project environment and optionally a running API and unit tests.
.EXAMPLE
.\scripts\check.ps1 -BaseUrl http://127.0.0.1:8000 -RequireAnalyzer
#>
[CmdletBinding()]
param(
    [string]$BaseUrl,
    [string]$EnvPath,
    [switch]$RequireAnalyzer,
    [switch]$AllowMissingMediaTools,
    [switch]$RunTests
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$null = Initialize-ProjectEnvironment -ProjectRoot $projectRoot -EnvPath $EnvPath
$pythonPath = Get-ProjectPython -ProjectRoot $projectRoot

if ([string]::IsNullOrWhiteSpace($env:APP_API_KEY)) {
    throw 'APP_API_KEY is missing. Run scripts/setup.ps1 first, or set a key in your existing .env.'
}

Push-Location -LiteralPath $projectRoot
try {
    Assert-ProjectDependencies -PythonPath $pythonPath -ProjectRoot $projectRoot
    Write-Host 'Python 3.12/3.13, pinned service dependencies, application settings and five-minute timeout budget: OK'

    $ffmpegPath = if ($env:APP_FFMPEG_BIN) { $env:APP_FFMPEG_BIN } else { 'ffmpeg' }
    $ffprobePath = if ($env:APP_FFPROBE_BIN) { $env:APP_FFPROBE_BIN } else { 'ffprobe' }
    $missingTools = @()
    if (-not (Test-MediaExecutable -Executable $ffmpegPath)) { $missingTools += 'FFmpeg' }
    if (-not (Test-MediaExecutable -Executable $ffprobePath)) { $missingTools += 'FFprobe' }
    if ($missingTools.Count -gt 0) {
        $message = 'Missing or invalid media tools: ' + ($missingTools -join ', ') + '. Set APP_FFMPEG_BIN and APP_FFPROBE_BIN to installed executable paths.'
        if ($AllowMissingMediaTools -and -not $RequireAnalyzer) { Write-Warning $message }
        else { throw $message }
    }
    else { Write-Host 'FFmpeg and FFprobe: OK' }

    if ([string]::IsNullOrWhiteSpace($env:APP_ANALYZER_FACTORY)) {
        if ($RequireAnalyzer) { throw 'APP_ANALYZER_FACTORY is missing. Connect the local ML adapter first.' }
        Write-Warning 'Local analyzer is not configured. The interface can run; analysis is unavailable.'
    }
    if ($RequireAnalyzer) {
        & $pythonPath -c 'from app.config import Settings; from app.rules import load_rule_catalog; catalog = load_rule_catalog(Settings.from_env().rule_catalog_path); assert catalog.enabled_rule_ids'
        if ($LASTEXITCODE -ne 0) { throw 'The rule catalog is invalid or has no enabled verified rules.' }
    }

    if ($BaseUrl) {
        $uri = $BaseUrl.TrimEnd('/') + '/api/v1/system'
        $state = Invoke-RestMethod -Method Get -Uri $uri -Headers @{ 'X-API-Key' = $env:APP_API_KEY } -TimeoutSec 15
        Write-Host ('API reachable. Ready for analysis: ' + $state.ready)
        Write-Host ('Time limits (seconds): upload=' + $state.limits.upload_timeout_sec + '; analysis=' + $state.limits.job_timeout_sec)
        if (-not $state.ready) {
            Write-Host ('Analyzer configured: ' + $state.analyzer.configured)
            Write-Host ('Checked rules ready: ' + $state.analyzer.rules_ready)
            Write-Host ('FFmpeg: ' + $state.tools.ffmpeg + '; FFprobe: ' + $state.tools.ffprobe)
            if ($RequireAnalyzer) { throw 'API is reachable but not ready for analysis.' }
        }
    }
    elseif ($RequireAnalyzer) {
        Write-Warning 'Factory syntax and rules are configured. Run this check with -BaseUrl after startup, then perform a real video analysis.'
    }

    if ($RunTests) {
        $node = Get-Command node -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $node) { throw 'Node.js 22 or newer is needed for -RunTests frontend checks. Normal server startup does not require Node.js.' }
        $nodeMajor = & $node.Source -p 'parseInt(process.versions.node)'
        if ($LASTEXITCODE -ne 0 -or [int]$nodeMajor -lt 22) { throw 'Frontend tests require Node.js 22 or newer. Normal server startup does not require Node.js.' }
        $frontendTests = @(Get-ChildItem -LiteralPath (Join-Path $projectRoot 'tests\frontend') -Filter '*.test.mjs' -File | ForEach-Object { $_.FullName })
        if ($frontendTests.Count -eq 0) { throw 'Frontend test files are missing.' }
        & $pythonPath -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw 'Project tests failed.' }
        & $node.Source --test @frontendTests
        if ($LASTEXITCODE -ne 0) { throw 'Frontend tests failed.' }
        & (Join-Path $PSScriptRoot 'test-launchers.ps1')
        Write-Host 'Service, frontend and launcher checks: OK'
    }
}
finally { Pop-Location }
