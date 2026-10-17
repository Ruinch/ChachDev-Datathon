<#
.SYNOPSIS
Checks the project environment and optionally a running API and unit tests.
.EXAMPLE
.\scripts\check.ps1 -BaseUrl http://127.0.0.1:8000 -RequireAnalyzer
#>
[CmdletBinding()]
param(
    [string]$BaseUrl,
    [switch]$RequireAnalyzer,
    [switch]$AllowMissingMediaTools,
    [switch]$RunTests
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
Import-ProjectEnvironment -Path (Join-Path $projectRoot '.env')
$pythonPath = Get-ProjectPython -ProjectRoot $projectRoot

if ([string]::IsNullOrWhiteSpace($env:APP_API_KEY)) {
    throw 'APP_API_KEY is missing. Set it in .env or the process environment; see .env.example.'
}

Push-Location -LiteralPath $projectRoot
try {
    & $pythonPath -c 'import fastapi, uvicorn, multipart, PIL; from app.config import Settings; from app.main import create_app; Settings.from_env().validate()'
    if ($LASTEXITCODE -ne 0) { throw 'Python dependencies or application settings are invalid.' }
    Write-Host 'Python, application settings and five-minute timeout budget: OK'

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
        & $pythonPath -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw 'Project tests failed.' }
    }
}
finally { Pop-Location }
