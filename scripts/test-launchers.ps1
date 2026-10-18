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
    'APP_ANALYSIS_TIMEOUT_SECONDS', 'APP_FFPROBE_TIMEOUT_SECONDS', 'APP_ANALYZER_FACTORY',
    'APP_LOCAL_MODEL_URL', 'APP_ML_INPUT_MODE', 'APP_ML_FRAMES', 'APP_ML_MAX_SIDE', 'APP_ML_BUDGET_SECONDS',
    'APP_LOCAL_MODEL_IMAGE_MIN_TOKENS', 'APP_LOCAL_MODEL_IMAGE_MAX_TOKENS'
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

        # Configuration mistakes must fail before opening or owning model processes.
        $blocked = $false
        try { & (Join-Path $PSScriptRoot 'start-ml.ps1') -EnvPath $testConfig -Port 8091 -ModelPort 8091 -CheckOnly }
        catch {
            if (-not $_.Exception.Message.Contains('must differ')) { throw }
            $blocked = $true
        }
        if (-not $blocked) { throw 'Combined launcher accepted identical API and model ports.' }

        foreach ($name in $trackedNames) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        $env:APP_ANALYZER_FACTORY = 'other.adapter:create'
        $blocked = $false
        try { & (Join-Path $PSScriptRoot 'start-ml.ps1') -EnvPath $testConfig -CheckOnly }
        catch {
            if (-not $_.Exception.Message.Contains('included local adapter only')) { throw }
            $blocked = $true
        }
        if (-not $blocked) { throw 'Combined launcher accepted an unrelated adapter.' }

        foreach ($name in $trackedNames) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        $env:APP_LOCAL_MODEL_URL = 'http://127.0.0.1:9999'
        $blocked = $false
        try { & (Join-Path $PSScriptRoot 'start-ml.ps1') -EnvPath $testConfig -CheckOnly }
        catch {
            if (-not $_.Exception.Message.Contains('differs from ModelPort')) { throw }
            $blocked = $true
        }
        if (-not $blocked) { throw 'Combined launcher accepted contradictory model ports.' }

        foreach ($name in $trackedNames) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        $tokenDefaults = Get-LocalModelImageTokenSettings
        if ($tokenDefaults.ImageMinTokens -ne 256 -or $tokenDefaults.ImageMaxTokens -ne 768) { throw 'Default image token limits changed.' }
        foreach ($invalid in @('63', '4097', '-1', '256.5', 'bad', '9999999999999999999999', ' ')) {
            $env:APP_LOCAL_MODEL_IMAGE_MAX_TOKENS = $invalid
            $blocked = $false
            try { & (Join-Path $PSScriptRoot 'start-ml.ps1') -EnvPath $testConfig -CheckOnly }
            catch {
                if (-not $_.Exception.Message.Contains('APP_LOCAL_MODEL_IMAGE_MAX_TOKENS must be an integer')) { throw }
                $blocked = $true
            }
            if (-not $blocked) { throw 'Invalid image token limit was accepted.' }
        }
        $env:APP_LOCAL_MODEL_IMAGE_MIN_TOKENS = '1024'
        $env:APP_LOCAL_MODEL_IMAGE_MAX_TOKENS = '768'
        $blocked = $false
        try { & (Join-Path $PSScriptRoot 'start-ml.ps1') -EnvPath $testConfig -CheckOnly }
        catch {
            if (-not $_.Exception.Message.Contains('must not exceed APP_LOCAL_MODEL_IMAGE_MAX_TOKENS')) { throw }
            $blocked = $true
        }
        if (-not $blocked) { throw 'Reversed image token limits were accepted.' }

        foreach ($name in $trackedNames) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        $reportPath = Join-Path $testDirectory 'environment-report.json'
        $env:APP_API_KEY = $createdKey
        $null = & (Join-Path $PSScriptRoot 'export-runtime-report.ps1') -EnvPath $testConfig -OutputPath $reportPath 6>&1
        if (([IO.File]::ReadAllText($reportPath)).Contains($createdKey)) { throw 'Environment report contains the access key.' }
        $reportHash = (Get-FileHash -LiteralPath $reportPath).Hash
        $blocked = $false
        try { & (Join-Path $PSScriptRoot 'export-runtime-report.ps1') -EnvPath $testConfig -OutputPath $reportPath 2>$null }
        catch { $blocked = $true }
        if (-not $blocked -or (Get-FileHash -LiteralPath $reportPath).Hash -ne $reportHash) { throw 'Environment report was silently overwritten.' }
        Remove-Item -LiteralPath $reportPath

        # Exercise the real integrity check and process-return boundary in a tiny
        # isolated project. The process starter is stubbed; no model is executed.
        $fixtureRoot = Join-Path $testDirectory 'model fixture with spaces'
        $pythonPath = Get-ProjectPython -ProjectRoot $projectRoot
        $fixtureCode = @'
import hashlib, json, shutil, sys, zipfile
from pathlib import Path
project, fixture = map(Path, sys.argv[1:])
for name in ("ml_tools", ".venv/Scripts", ".venv/Lib/site-packages", ".local_ml/models", ".local_ml/downloads", ".local_ml/llama"):
    (fixture / name).mkdir(parents=True, exist_ok=True)
(fixture / ".venv/Lib/site-packages/av.py").write_text("# Synthetic import stub: no decoding occurs in launcher tests.\n", encoding="utf-8")
for name in ("ml_tools/start_model.ps1", "ml_tools/install_runtime.py", ".venv/Scripts/python.exe", ".venv/pyvenv.cfg"):
    shutil.copyfile(project / name, fixture / name)
weights = {"Qwen3.5-4B-Q4_K_M.gguf": b"synthetic weights", "mmproj-F16.gguf": b"synthetic projector"}
archive = fixture / ".local_ml/downloads/runtime.zip"
with zipfile.ZipFile(archive, "w") as bundle:
    bundle.writestr("llama-server.exe", b"synthetic non-executable runtime")
(fixture / ".local_ml/llama/llama-server.exe").write_bytes(b"synthetic non-executable runtime")
files = [{"name": "runtime.zip", "kind": "runtime", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "url": "https://example.invalid/runtime.zip"}]
for name, content in weights.items():
    (fixture / ".local_ml/models" / name).write_bytes(content)
    files.append({"name": name, "kind": "model", "sha256": hashlib.sha256(content).hexdigest(), "url": "https://example.invalid/" + name})
(fixture / "ml_tools/model35-lock.json").write_text(json.dumps({"files": files}), encoding="utf-8")
'@
        $fixtureCode | & $pythonPath - $projectRoot $fixtureRoot
        if ($LASTEXITCODE -ne 0) { throw 'Could not prepare the isolated launcher fixture.' }

        # Test the combined launcher's real profile loading and forwarding, while
        # replacing environment/model checks with local stubs. No server starts.
        $fixtureScripts = Join-Path $fixtureRoot 'scripts'
        $null = New-Item -ItemType Directory -Path $fixtureScripts
        foreach ($script in @('common.ps1', 'start-ml.ps1')) {
            Copy-Item -LiteralPath (Join-Path $PSScriptRoot $script) -Destination (Join-Path $fixtureScripts $script)
        }
        [IO.File]::WriteAllText((Join-Path $fixtureScripts 'check.ps1'), 'param($EnvPath, [switch]$RequireAnalyzer)')
        $fixtureModel = Join-Path $fixtureRoot 'ml_tools/start_model.ps1'
        $realFixtureModel = [IO.File]::ReadAllText($fixtureModel)
        $stubModel = @'
param($Model, $Port, $ImageMinTokens, $ImageMaxTokens, [switch]$CheckOnly, [switch]$Background)
if (-not $CheckOnly -or $Background) { throw 'Combined fixture attempted to start a model.' }
if ($Model -ne '3.5-4b' -or $Port -ne 8091) { throw 'Combined launcher changed the model or port.' }
return [pscustomobject]@{ Min = $ImageMinTokens; Max = $ImageMaxTokens }
'@
        try {
            [IO.File]::WriteAllText($fixtureModel, $stubModel)
            foreach ($name in $trackedNames) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
            $forwarded = & (Join-Path $fixtureScripts 'start-ml.ps1') -EnvPath $testConfig -CheckOnly
            if ($forwarded.Min -ne 256 -or $forwarded.Max -ne 768) { throw 'Default image token limits were not forwarded.' }
            $tokenConfig = Join-Path $testDirectory 'image-tokens.env'
            [IO.File]::WriteAllText($tokenConfig, "APP_LOCAL_MODEL_IMAGE_MIN_TOKENS=512`nAPP_LOCAL_MODEL_IMAGE_MAX_TOKENS=1024`n")
            $profileHash = (Get-FileHash -LiteralPath $tokenConfig).Hash
            $forwarded = & (Join-Path $fixtureScripts 'start-ml.ps1') -EnvPath $tokenConfig -CheckOnly
            if ($forwarded.Min -ne 512 -or $forwarded.Max -ne 1024) { throw 'Profile image token limits were not forwarded.' }
            $env:APP_LOCAL_MODEL_IMAGE_MAX_TOKENS = '1536'
            $forwarded = & (Join-Path $fixtureScripts 'start-ml.ps1') -EnvPath $tokenConfig -CheckOnly
            if ($forwarded.Min -ne 512 -or $forwarded.Max -ne 1536) { throw 'Explicit process environment precedence changed.' }
            if ((Get-FileHash -LiteralPath $tokenConfig).Hash -ne $profileHash) { throw 'Image token settings changed the profile on disk.' }
        }
        finally {
            [IO.File]::WriteAllText($fixtureModel, $realFixtureModel)
            foreach ($name in $trackedNames) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        }
        $modelProbe = @{ Starts = 0; ContextCheckpoints = '32' }
        function Start-Process {
            param($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru)
            $modelProbe.Starts++
            if ($WindowStyle -ne 'Hidden' -or -not $PassThru) { throw 'Background runtime must be hidden and return its owned process.' }
            if ($ArgumentList -notcontains '--offline') { throw 'Runtime omitted offline mode.' }
            foreach ($flag in @('--cache-ram', '--ctx-checkpoints')) {
                $flagIndex = [array]::IndexOf($ArgumentList, $flag)
                $expected = if ($flag -eq '--ctx-checkpoints') { $modelProbe.ContextCheckpoints } else { '0' }
                if ($flagIndex -lt 0 -or $flagIndex + 1 -ge $ArgumentList.Count -or $ArgumentList[$flagIndex + 1] -ne $expected) {
                    throw "Runtime did not pass the required value for $flag."
                }
            }
            if ($ArgumentList -notcontains '--no-cache-idle-slots') { throw 'Runtime must not retain idle slot prompt state.' }
            return [pscustomobject]@{ FixtureProcess = $true; HasExited = $false; Id = 0 }
        }
        try {
            $returned = @(& (Join-Path $fixtureRoot 'ml_tools/start_model.ps1') -Background)
            if ($returned.Count -ne 1 -or -not $returned[0].FixtureProcess) { throw 'Background runtime polluted its process return value with verification output.' }
            $null = & (Join-Path $fixtureRoot 'ml_tools/start_model.ps1') -CheckOnly
            if ($modelProbe.Starts -ne 1) { throw 'CheckOnly unexpectedly started a process.' }
            $modelProbe.ContextCheckpoints = '0'
            $null = & (Join-Path $fixtureRoot 'ml_tools/start_model.ps1') -Background -ContextCheckpoints 0
            if ($modelProbe.Starts -ne 2) { throw 'Explicit checkpoint override was not passed to the owned runtime.' }
            [IO.File]::WriteAllText((Join-Path $fixtureRoot '.local_ml/models/Qwen3.5-4B-Q4_K_M.gguf'), 'corrupt synthetic weights')
            $blocked = $false
            try { & (Join-Path $fixtureRoot 'ml_tools/start_model.ps1') -Background 2>$null }
            catch { $blocked = $true }
            if (-not $blocked -or $modelProbe.Starts -ne 2) { throw 'Runtime started with corrupt local weights.' }
        }
        finally { Remove-Item Function:\Start-Process }
    }
    finally { Pop-Location }

    if ($rootConfigHash) {
        if ((Get-FileHash -LiteralPath $rootConfig).Hash -ne $rootConfigHash) { throw 'Project .env was modified.' }
    }
    elseif (Test-Path -LiteralPath $rootConfig) { throw 'The test unexpectedly created the project .env.' }
    Write-Host 'Launcher checks passed: secret handling, repeatable setup, foreign working directory, paths, model port/factory guards and sanitized immutable environment reports.'
}
finally {
    foreach ($name in $trackedNames) { [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process') }
    # Recursive cleanup is restricted to this newly created GUID directory.
    $resolvedTestDirectory = [IO.Path]::GetFullPath($testDirectory)
    $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolvedTestDirectory.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $resolvedTestDirectory) -notmatch '^cash-audit-launchers-[0-9a-f]{32}$') {
        throw 'Refusing cleanup outside the isolated launcher test directory.'
    }
    if (Test-Path -LiteralPath $resolvedTestDirectory -PathType Container) { Remove-Item -LiteralPath $resolvedTestDirectory -Recurse }
}
