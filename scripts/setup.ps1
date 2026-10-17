<#
.SYNOPSIS
Prepares the validated Windows service environment. Does not install models.
.EXAMPLE
.\scripts\setup.ps1
.EXAMPLE
.\scripts\setup.ps1 -SkipInstall -EnvPath .local_data\setup-check.env
#>
[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [string]$EnvPath,
    [switch]$AllowMissingMediaTools
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPath = if ($EnvPath) { Resolve-ProjectPath -ProjectRoot $projectRoot -Path $EnvPath } else { Join-Path $projectRoot '.env' }
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    if ($SkipInstall) { throw 'Project .venv is missing. Run setup.ps1 without -SkipInstall to create it.' }
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Python launcher not found. Install Python 3.13 for Windows with the launcher, then run setup.ps1 again.' }
    & py -3.13 -c 'import sys; assert sys.version_info[:2] == (3, 13)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.13 is unavailable. Install it, then run setup.ps1 again.' }
    & py -3.13 -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
}

$versionCheck = @'
import sys
if sys.version_info[:2] not in {(3, 12), (3, 13)}:
    raise SystemExit("Existing .venv requires Python 3.12 or 3.13. Preserve other environments before setup.")
'@
$versionCheck | & $pythonPath -
if ($LASTEXITCODE -ne 0) { throw 'Unsupported virtual environment. Dependencies and configuration were not changed.' }

Push-Location -LiteralPath $projectRoot
try {
    if (-not $SkipInstall) {
        & $pythonPath -m pip install --disable-pip-version-check -c (Join-Path $projectRoot 'constraints.txt') -e '.[test]'
        if ($LASTEXITCODE -ne 0) { throw 'Service dependency installation failed. Existing configuration was preserved.' }
        & $pythonPath -m pip check
        if ($LASTEXITCODE -ne 0) { throw 'Installed package requirements conflict. Resolve the reported service/ML environment conflict before startup.' }
    }

    if (-not (Test-Path -LiteralPath $environmentPath)) {
        $parentDirectory = Split-Path -Parent $environmentPath
        if (-not (Test-Path -LiteralPath $parentDirectory -PathType Container)) {
            $null = New-Item -ItemType Directory -Path $parentDirectory -Force
        }
        $key = [Environment]::GetEnvironmentVariable('APP_API_KEY', 'Process')
        if ([string]::IsNullOrWhiteSpace($key)) {
            $bytes = New-Object byte[] 32
            $random = [Security.Cryptography.RandomNumberGenerator]::Create()
            try { $random.GetBytes($bytes) } finally { $random.Dispose() }
            $key = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
        }
        if ($key.Contains("`r") -or $key.Contains("`n")) { throw 'APP_API_KEY must be a single-line value.' }
        $template = [IO.File]::ReadAllText((Join-Path $projectRoot '.env.example'))
        $configuration = [regex]::Replace($template, '(?m)^APP_API_KEY=.*$', { param($match) 'APP_API_KEY=' + $key })
        # CreateNew protects a configuration created concurrently; never truncate an existing file.
        $stream = [IO.File]::Open($environmentPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $content = (New-Object Text.UTF8Encoding($false)).GetBytes($configuration)
            $stream.Write($content, 0, $content.Length)
        }
        finally { $stream.Dispose() }
        Write-Host 'Local configuration created. The access key is stored in that file and is not printed.'
    }
    else { Write-Host 'Existing configuration preserved.' }

    $null = Initialize-ProjectEnvironment -ProjectRoot $projectRoot -EnvPath $environmentPath
    if ([string]::IsNullOrWhiteSpace($env:APP_API_KEY)) {
        throw 'Existing configuration has no access key. Set APP_API_KEY in that file or the process environment; setup does not overwrite an existing configuration.'
    }
    & (Join-Path $PSScriptRoot 'check.ps1') -EnvPath $environmentPath -AllowMissingMediaTools:$AllowMissingMediaTools
    Write-Host 'Service environment prepared. Run scripts/start.ps1 to open the interface; use the same -EnvPath when using a separate configuration.'
}
finally { Pop-Location }
