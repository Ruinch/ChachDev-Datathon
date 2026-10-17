Set-StrictMode -Version Latest

function Import-ProjectEnvironment {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $lineNumber++
        $entry = $line.Trim()
        if (-not $entry -or $entry.StartsWith('#')) { continue }
        if ($entry -notmatch '^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            throw "Invalid .env entry at line $lineNumber. Expected NAME=value."
        }
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        )) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name, 'Process'))) {
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }
}

function Get-ProjectPython {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $pythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw 'Project .venv is missing. Run scripts/setup.ps1 first.'
    }
    return $pythonPath
}

function Resolve-ProjectPath {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Path
    )

    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path $ProjectRoot $Path))
}

function Initialize-ProjectEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string]$EnvPath
    )

    $environmentPath = if ($EnvPath) { Resolve-ProjectPath -ProjectRoot $ProjectRoot -Path $EnvPath } else { Join-Path $ProjectRoot '.env' }
    Import-ProjectEnvironment -Path $environmentPath

    # Resolve before validation or changing location: every launcher uses the same base.
    $dataPath = if ($env:APP_DATA_DIR) { $env:APP_DATA_DIR } else { '.local_data' }
    $env:APP_DATA_DIR = Resolve-ProjectPath -ProjectRoot $ProjectRoot -Path $dataPath
    if ($env:APP_RULE_CATALOG_PATH) {
        $env:APP_RULE_CATALOG_PATH = Resolve-ProjectPath -ProjectRoot $ProjectRoot -Path $env:APP_RULE_CATALOG_PATH
    }

    foreach ($tool in @('ffmpeg', 'ffprobe')) {
        $name = 'APP_' + $tool.ToUpperInvariant() + '_BIN'
        $configured = [Environment]::GetEnvironmentVariable($name, 'Process')
        $localTool = Join-Path $ProjectRoot ".local_tools\ffmpeg\bin\${tool}.exe"
        if ((-not $configured -or $configured -eq $tool) -and (Test-Path -LiteralPath $localTool -PathType Leaf)) {
            [Environment]::SetEnvironmentVariable($name, $localTool, 'Process')
        }
        elseif ($configured -and ($configured.Contains('\') -or $configured.Contains('/'))) {
            [Environment]::SetEnvironmentVariable($name, (Resolve-ProjectPath -ProjectRoot $ProjectRoot -Path $configured), 'Process')
        }
    }
    return $environmentPath
}

function Assert-ProjectDependencies {
    param([Parameter(Mandatory = $true)][string]$PythonPath, [Parameter(Mandatory = $true)][string]$ProjectRoot)

    $validation = @'
import importlib.metadata
import pathlib
import sys

if sys.version_info[:2] != (3, 13):
    raise SystemExit("The validated Windows service environment requires Python 3.13; recreate .venv with that version.")
errors = []
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    name, version = line.split("==", 1)
    try:
        actual = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        actual = "missing"
    if actual != version:
        errors.append(f"{name}: expected {version}, installed {actual}")
if errors:
    raise SystemExit("Service dependency versions differ from constraints.txt:\n" + "\n".join(errors))
import fastapi, uvicorn, multipart, PIL, pytest, httpx
from app.config import Settings
Settings.from_env().validate()
'@
    # stdin avoids Windows PowerShell 5.1 removing nested quotes from a -c argument.
    $validation | & $PythonPath - (Join-Path $ProjectRoot 'constraints.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Environment validation failed. Run scripts/setup.ps1 to install the validated service dependencies; review ML package compatibility before changing a shared ML environment.' }
}

function Test-MediaExecutable {
    param([Parameter(Mandatory = $true)][string]$Executable)

    try {
        & $Executable -version 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch { return $false }
}
