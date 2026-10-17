<#
.SYNOPSIS
Exercises Windows launcher setup without network installs or changing the project .env.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$trackedNames = @(
    'APP_API_KEY', 'APP_DATA_DIR', 'APP_RULE_CATALOG_PATH', 'APP_FFMPEG_BIN', 'APP_FFPROBE_BIN',
    'APP_MAX_UPLOAD_MB', 'APP_MAX_VIDEO_SECONDS', 'APP_UPLOAD_TIMEOUT_SECONDS',
    'APP_ANALYSIS_TIMEOUT_SECONDS', 'APP_FFPROBE_TIMEOUT_SECONDS', 'APP_ANALYZER_FACTORY'
)
$originalEnvironment = @{}
foreach ($name in $trackedNames) {
    $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}
$testDirectory = Join-Path ([IO.Path]::GetTempPath()) ('cash-audit-launchers-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $testDirectory
$testConfig = Join-Path $testDirectory 'service.env'
$emptyConfig = Join-Path $testDirectory 'empty.env'
$rootConfig = Join-Path $projectRoot '.env'
$rootConfigHash = if (Test-Path -LiteralPath $rootConfig -PathType Leaf) { (Get-FileHash -LiteralPath $rootConfig).Hash } else { $null }

try {
    Push-Location -LiteralPath $testDirectory
    try {
        $output = & (Join-Path $PSScriptRoot 'setup.ps1') -SkipInstall -EnvPath $testConfig -AllowMissingMediaTools 6>&1
        $keyLine = [IO.File]::ReadAllLines($testConfig) | Where-Object { $_ -match '^APP_API_KEY=' } | Select-Object -First 1
        $createdKey = $keyLine.Substring('APP_API_KEY='.Length)
        if ($createdKey -notmatch '^[0-9a-f]{64}$') { throw 'Setup did not generate a 256-bit random key.' }
        if (($output | Out-String).Contains($createdKey)) { throw 'Setup printed the generated access key.' }
        if ($env:APP_DATA_DIR -ne (Join-Path $projectRoot '.local_data')) { throw 'Data directory depends on the caller working directory.' }
        $firstHash = (Get-FileHash -LiteralPath $testConfig).Hash
        $null = & (Join-Path $PSScriptRoot 'setup.ps1') -SkipInstall -EnvPath $testConfig -AllowMissingMediaTools 6>&1
        if ((Get-FileHash -LiteralPath $testConfig).Hash -ne $firstHash) { throw 'Repeated setup changed an existing configuration.' }

        foreach ($name in $trackedNames) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        $env:APP_RULE_CATALOG_PATH = 'app/data/rules.json'
        $null = Initialize-ProjectEnvironment -ProjectRoot $projectRoot -EnvPath $testConfig
        if ($env:APP_RULE_CATALOG_PATH -ne (Join-Path $projectRoot 'app\data\rules.json')) { throw 'Rule catalog depends on the caller working directory.' }
        foreach ($tool in @('ffmpeg', 'ffprobe')) {
            $localTool = Join-Path $projectRoot ".local_tools\ffmpeg\bin\${tool}.exe"
            if (Test-Path -LiteralPath $localTool -PathType Leaf) {
                $configured = [Environment]::GetEnvironmentVariable(('APP_' + $tool.ToUpperInvariant() + '_BIN'), 'Process')
                if ($configured -ne $localTool) { throw 'Local media executable was not discovered.' }
            }
        }

        [IO.File]::WriteAllText($emptyConfig, "APP_API_KEY=`n")
        foreach ($name in $trackedNames) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        $emptyHash = (Get-FileHash -LiteralPath $emptyConfig).Hash
        $blocked = $false
        try { & (Join-Path $PSScriptRoot 'start.ps1') -EnvPath $emptyConfig }
        catch {
            if (-not $_.Exception.Message.Contains('Run scripts/setup.ps1')) { throw }
            $blocked = $true
        }
        if (-not $blocked) { throw 'Startup accepted a missing access key.' }
        if ((Get-FileHash -LiteralPath $emptyConfig).Hash -ne $emptyHash) { throw 'Launcher changed an existing empty configuration.' }
    }
    finally { Pop-Location }

    if ($rootConfigHash) {
        if ((Get-FileHash -LiteralPath $rootConfig).Hash -ne $rootConfigHash) { throw 'Project .env was modified.' }
    }
    elseif (Test-Path -LiteralPath $rootConfig) { throw 'The test unexpectedly created the project .env.' }
    Write-Host 'Launcher checks passed: secret handling, repeatable setup, foreign working directory, relative paths, local tool discovery and missing-key refusal.'
}
finally {
    foreach ($name in $trackedNames) { [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process') }
    foreach ($path in @($testConfig, $emptyConfig)) { if (Test-Path -LiteralPath $path -PathType Leaf) { Remove-Item -LiteralPath $path } }
    if (Test-Path -LiteralPath $testDirectory -PathType Container) { Remove-Item -LiteralPath $testDirectory }
}
