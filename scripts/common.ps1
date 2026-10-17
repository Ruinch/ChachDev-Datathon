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
        throw 'Project .venv is missing. Create it and install the project as described in README.md.'
    }
    return $pythonPath
}

function Test-MediaExecutable {
    param([Parameter(Mandatory = $true)][string]$Executable)

    try {
        & $Executable -version 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch { return $false }
}
