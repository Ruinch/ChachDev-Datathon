from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows owner-operated recovery guards')
def test_recovery_stops_only_the_same_verified_model_and_rejects_foreign_or_reused_process(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / 'synthetic-recovery.ps1'
    script.write_text(r'''
param($GuardPath)
$ErrorActionPreference = 'Stop'
. $GuardPath
$root = 'C:\synthetic project'
$runtime = Join-Path $root '.local_ml\llama\llama-server.exe'
$weights = Join-Path $root '.local_ml\models\Qwen3.5-4B-Q4_K_M.gguf'
$projector = Join-Path $root '.local_ml\models\mmproj-F16.gguf'
$validCommand = '"' + $runtime + '" -m "' + $weights + '" --mmproj "' + $projector + '" --host 127.0.0.1 --port 8091 --alias local-cash-vlm'
$probe = @{ Stops = @(); Address = '127.0.0.1'; Listener = $true; Pid = 101; Created = [datetime]'2026-01-01'; Exe = $runtime; Command = $validCommand; Evaluation = $false; ReplaceOnSecondRead = $false; Reads = 0 }
function Get-NetTCPConnection { param($LocalPort,$State,$ErrorAction); if ($probe.Listener) { [pscustomobject]@{ LocalAddress=$probe.Address; OwningProcess=$probe.Pid } } }
function Get-CimInstance {
    param($ClassName,$Filter)
    if ($Filter -like 'Name =*') {
        if ($probe.Evaluation) { [pscustomobject]@{ CommandLine='python -m ml_tools.evaluate --endpoint http://127.0.0.1:8091' } }
        return
    }
    $probe.Reads++
    $created = if ($probe.ReplaceOnSecondRead -and $probe.Reads -gt 1) { $probe.Created.AddSeconds(1) } else { $probe.Created }
    return [pscustomobject]@{ ProcessId=$probe.Pid; CreationDate=$created; ExecutablePath=$probe.Exe; CommandLine=$probe.Command }
}
function Stop-Process { param($Id); $probe.Stops += $Id }
function Expect-Blocked([scriptblock]$Action) {
    $blocked=$false
    try { & $Action | Out-Null } catch { $blocked=$true }
    if (-not $blocked) { throw 'Unsafe synthetic recovery was accepted.' }
}
$snapshot=Get-VerifiedProjectModel -ProjectRoot $root
foreach ($bad in @(
    $validCommand.Replace('8091','8092'),
    $validCommand.Replace('local-cash-vlm','other-model'),
    $validCommand.Replace('Qwen3.5-4B-Q4_K_M.gguf','different.gguf'),
    $validCommand.Replace('mmproj-F16.gguf','different-projector.gguf'),
    ($validCommand + ' --port 8092'),
    ($validCommand + ' -m=other'),
    ($validCommand + ' --model=other'),
    ($validCommand + ' --mmproj=other'),
    ($validCommand + ' --host=0.0.0.0'),
    ($validCommand + ' --port=8092'),
    ($validCommand + ' --alias=other'),
    ($validCommand + ' -hf=other'),
    ($validCommand + ' --hf-repo=other')
)) {
    $probe.Command=$bad
    Expect-Blocked { Stop-VerifiedProjectModel -ProjectRoot $root -ExpectedSnapshot $snapshot }
}
$probe.Command=$validCommand
$probe.Exe='C:\foreign\llama-server.exe'
Expect-Blocked { Stop-VerifiedProjectModel -ProjectRoot $root -ExpectedSnapshot $snapshot }
$probe.Exe=$runtime
$probe.Address='0.0.0.0'
Expect-Blocked { Stop-VerifiedProjectModel -ProjectRoot $root -ExpectedSnapshot $snapshot }
$probe.Address='127.0.0.1'
$probe.Created=$probe.Created.AddSeconds(1)
Expect-Blocked { Stop-VerifiedProjectModel -ProjectRoot $root -ExpectedSnapshot $snapshot }
$probe.Created=$snapshot.CreationDate
$probe.Reads=0; $probe.ReplaceOnSecondRead=$true
Expect-Blocked { Stop-VerifiedProjectModel -ProjectRoot $root -ExpectedSnapshot $snapshot }
$probe.ReplaceOnSecondRead=$false
$probe.Listener=$false
Stop-VerifiedProjectModel -ProjectRoot $root -ExpectedSnapshot $snapshot
if ($probe.Stops.Count) { throw 'A foreign, reused or absent model was stopped.' }
$probe.Listener=$true
Stop-VerifiedProjectModel -ProjectRoot $root -ExpectedSnapshot $snapshot
if ($probe.Stops.Count -ne 1 -or $probe.Stops[0] -ne 101) { throw 'The verified owned model was not stopped exactly once.' }
$probe.Evaluation=$true
Expect-Blocked { Assert-NoLocalEvaluation }
$probe.Evaluation=$false
Assert-NoLocalEvaluation
Write-Output 'Synthetic recovery guards passed; no real process was touched.'
''', encoding='utf-8')
    completed = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script),
         '-GuardPath', str(root / 'scripts/recovery-model.ps1')],
        capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert 'Synthetic recovery guards passed' in completed.stdout
