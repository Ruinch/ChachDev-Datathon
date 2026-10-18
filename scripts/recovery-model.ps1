<# Read-only ownership guards used by the owner-operated restart. #>
function Assert-NoLocalEvaluation {
    $evaluators = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" | Where-Object {
        $_.CommandLine -match '(?:^|\s)-m\s+ml_tools\.evaluate(?:\s|$)' -or
        $_.CommandLine -match '[\\/]ml_tools[\\/]evaluate\.py(?:"|\s|$)' -or
        $_.CommandLine -match '[\\/]v16_local_e2e\.py(?:"|\s|$)'
    })
    if ($evaluators.Count) { throw 'A local evaluation or functional model probe is running. Nothing was stopped.' }
}

function Assert-ProjectModelIdentity {
    param([Parameter(Mandatory = $true)]$Process, [Parameter(Mandatory = $true)][string]$ProjectRoot)
    $runtime = [IO.Path]::GetFullPath((Join-Path $ProjectRoot '.local_ml\llama\llama-server.exe'))
    if (-not $Process -or $Process.ExecutablePath -ne $runtime -or -not $Process.CreationDate) {
        throw 'Model ownership is not verified. No unverified model will be stopped.'
    }
    $tokens = @([regex]::Matches($Process.CommandLine, '"[^"]*"|\S+') | ForEach-Object { $_.Value.Trim('"') })
    if (@($tokens | Where-Object { $_ -match '^(?:-m|--model|--mmproj|--host|--port|--alias|-hf|--hf-repo)=' }).Count) {
        throw 'Ambiguous equals-form model arguments. No unverified model will be stopped.'
    }
    $expected = @{
        '-m' = (Join-Path $ProjectRoot '.local_ml\models\Qwen3.5-4B-Q4_K_M.gguf')
        '--mmproj' = (Join-Path $ProjectRoot '.local_ml\models\mmproj-F16.gguf')
        '--host' = '127.0.0.1'; '--port' = '8091'; '--alias' = 'local-cash-vlm'
    }
    foreach ($flag in $expected.Keys) {
        $positions = @(for ($i = 1; $i -lt $tokens.Count; $i++) { if ($tokens[$i] -eq $flag) { $i } })
        if ($positions.Count -ne 1 -or $positions[0] + 1 -ge $tokens.Count) { throw 'Model launch arguments do not match the pinned project runtime.' }
        $actual = $tokens[$positions[0] + 1]
        if ($flag -in @('-m', '--mmproj')) { $actual = [IO.Path]::GetFullPath($actual) }
        if ($actual -ne $expected[$flag]) { throw 'Model launch arguments do not match the pinned project runtime.' }
    }
    if ($tokens -contains '--model' -or $tokens -contains '-hf' -or $tokens -contains '--hf-repo') {
        throw 'Ambiguous model launch arguments. No unverified model will be stopped.'
    }
    return [pscustomobject]@{ ProcessId = $Process.ProcessId; CreationDate = $Process.CreationDate; ExecutablePath = $runtime }
}

function Get-VerifiedProjectModel {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot, $ExpectedSnapshot)
    $listeners = @(Get-NetTCPConnection -LocalPort 8091 -State Listen -ErrorAction SilentlyContinue)
    if (-not $listeners.Count) { return $null }
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') {
        throw 'Expected one project model on loopback port 8091. No unverified model will be stopped.'
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listeners[0].OwningProcess)"
    $snapshot = Assert-ProjectModelIdentity -Process $process -ProjectRoot $ProjectRoot
    if ($ExpectedSnapshot -and ($snapshot.ProcessId -ne $ExpectedSnapshot.ProcessId -or
        $snapshot.CreationDate -ne $ExpectedSnapshot.CreationDate)) {
        throw 'Model process changed during restart. No replacement process will be stopped.'
    }
    return $snapshot
}

function Stop-VerifiedProjectModel {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot, [Parameter(Mandatory = $true)]$ExpectedSnapshot)
    $verified = Get-VerifiedProjectModel -ProjectRoot $ProjectRoot -ExpectedSnapshot $ExpectedSnapshot
    if (-not $verified) { return }
    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($verified.ProcessId)"
    $identity = Assert-ProjectModelIdentity -Process $current -ProjectRoot $ProjectRoot
    if ($identity.ProcessId -ne $ExpectedSnapshot.ProcessId -or $identity.CreationDate -ne $ExpectedSnapshot.CreationDate) {
        throw 'Model identity changed before shutdown. Nothing else was stopped.'
    }
    Stop-Process -Id $identity.ProcessId
}
