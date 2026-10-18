<# Owner-operated restart of this project's idle laptop API and its launcher. #>
[CmdletBinding()]
param(
    [string]$ProfilePath,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
. (Join-Path $PSScriptRoot 'recovery-model.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$ProfilePath = Resolve-LaptopProfilePath -ProjectRoot $projectRoot -ProfilePath $ProfilePath
$manifest = Get-Content -LiteralPath (Join-Path $ProfilePath 'profile.json') -Raw | ConvertFrom-Json
$envPath = Join-Path $ProfilePath 'service.env'
if ($manifest.purpose -ne 'jury' -or $manifest.env_path -ne $envPath) { throw 'Unexpected laptop profile.' }
$profileValues = @{}
foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
    if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        $profileValues[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
    }
}
if (-not $profileValues['APP_API_KEY']) { throw 'Profile key is missing.' }
$listeners = @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') {
    throw 'Expected exactly one loopback API on port 8000. Nothing was stopped.'
}
$apiProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($listeners[0].OwningProcess)"
$wrapper = Get-CimInstance Win32_Process -Filter "ProcessId = $($apiProcess.ParentProcessId)"
$expectedPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if ($apiProcess.CommandLine -notmatch '-m\s+uvicorn\s+app.main:app' -or
    $apiProcess.CommandLine -notmatch '--port\s+8000' -or
    ($apiProcess.ExecutablePath -ne $expectedPython -and $wrapper.ExecutablePath -ne $expectedPython)) {
    throw 'API process does not belong to this project. Nothing was stopped.'
}
$request = [Net.HttpWebRequest]::Create('http://127.0.0.1:8000/api/v1/system')
$request.Proxy = $null
$request.AllowAutoRedirect = $false
$request.Timeout = 5000
$request.Headers.Add('X-API-Key', $profileValues['APP_API_KEY'])
$response = $request.GetResponse()
try {
    $reader = New-Object IO.StreamReader($response.GetResponseStream())
    try { $state = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
} finally { $response.Dispose() }
if ($state.busy) { throw 'An analysis is active. Wait for it to finish before restarting.' }
Assert-NoLocalEvaluation
$modelBefore = Get-VerifiedProjectModel -ProjectRoot $projectRoot
if ($CheckOnly) {
    Write-Host 'Idle project API, model ownership and absence of evaluation processes verified. No processes stopped or started.'
    return
}
# Verify again immediately before mutation. The owner invokes this script only
# after evaluation has finished; it never kills a tree or an unknown runtime.
Assert-NoLocalEvaluation
$modelImmediate = Get-VerifiedProjectModel -ProjectRoot $projectRoot -ExpectedSnapshot $modelBefore
if (-not $modelBefore -and $modelImmediate) { throw 'A model appeared during restart preflight. Nothing was stopped.' }
$apiNow = Get-CimInstance Win32_Process -Filter "ProcessId = $($apiProcess.ProcessId)"
if (-not $apiNow -or $apiNow.CreationDate -ne $apiProcess.CreationDate -or $apiNow.ExecutablePath -ne $apiProcess.ExecutablePath) {
    throw 'API process changed during restart. Nothing was stopped.'
}
Stop-Process -Id $apiProcess.ProcessId
$deadline = [DateTime]::UtcNow.AddSeconds(20)
do {
    Start-Sleep -Milliseconds 400
    $remaining = @(Get-NetTCPConnection -LocalPort 8000,8091 -State Listen -ErrorAction SilentlyContinue)
} while ($remaining.Count -and [DateTime]::UtcNow -lt $deadline)
if (@($remaining | Where-Object { $_.LocalPort -eq 8000 }).Count) { throw 'A previous API is still listening. Nothing else was stopped.' }
if (@($remaining | Where-Object { $_.LocalPort -eq 8091 }).Count) {
    if (-not $modelBefore) { throw 'A model appeared during restart. Nothing else was stopped.' }
    Assert-NoLocalEvaluation
    # A manually restored model may outlive the old launcher. Stop only the
    # exact same verified PID and creation time captured before API shutdown.
    Stop-VerifiedProjectModel -ProjectRoot $projectRoot -ExpectedSnapshot $modelBefore
    $modelDeadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 200
        $remaining = @(Get-NetTCPConnection -LocalPort 8000,8091 -State Listen -ErrorAction SilentlyContinue)
    } while ($remaining.Count -and [DateTime]::UtcNow -lt $modelDeadline)
    if ($remaining.Count) { throw 'A server is still listening. No additional process was stopped.' }
}
$logPath = Join-Path $ProfilePath ('logs\owner-restart-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$null = New-Item -ItemType Directory -Path $logPath
$process = Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -ArgumentList @('-NoProfile', '-File', ('"{0}"' -f (Join-Path $PSScriptRoot 'run-laptop.ps1')),
        '-ProfilePath', ('"{0}"' -f $ProfilePath)) `
    -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logPath 'stdout.log') `
    -RedirectStandardError (Join-Path $logPath 'stderr.log')
Write-Host ('Laptop startup started. Supervisor PID: ' + $process.Id)
Write-Host 'Wait for the model to load, then refresh http://127.0.0.1:8000/ and reconnect with the existing key.'
