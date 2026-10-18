<# Scheduled-task entry point. Keeps startup stdout/stderr in the private profile. #>
[CmdletBinding()]
param(
    [string]$ProfilePath,
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$projectRoot = Split-Path -Parent $PSScriptRoot
$exitCode = 1
$transcribing = $false
try {
    $ProfilePath = Resolve-LaptopProfilePath -ProjectRoot $projectRoot -ProfilePath $ProfilePath
    $logsPath = Join-Path $ProfilePath 'logs'
    if (-not (Test-Path -LiteralPath (Join-Path $ProfilePath 'profile.json') -PathType Leaf)) {
        throw 'Prepare a private jury profile before running the laptop launcher.'
    }
    # A transcript may contain local paths and diagnostics. Refuse broadly readable folders.
    # OWNER RIGHTS is the file owner's well-known identity, not a second account.
    $allowedSids = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-4')
    foreach ($folder in @($ProfilePath, $logsPath)) {
        $item = Get-Item -LiteralPath $folder
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Private log folders cannot be reparse points.' }
        foreach ($rule in [IO.Directory]::GetAccessControl($folder).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            if ($rule.AccessControlType -eq 'Allow' -and $sid -notin $allowedSids) {
                throw 'Private profile/log permissions allow another account. Review ACLs before logging.'
            }
        }
    }
    $runPath = Join-Path $logsPath ('startup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $runPath
    $null = Start-Transcript -LiteralPath (Join-Path $runPath 'transcript.log')
    $transcribing = $true
    Write-Host "Laptop startup diagnostics: $runPath"
    $launcher = Join-Path $PSScriptRoot 'start-jury.ps1'
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $launcher),
                   '-ProfilePath', ('"{0}"' -f $ProfilePath), '-InterfaceOnly', '-StartModel')
    if ($CheckOnly) { $arguments += '-CheckOnly' }
    # Native redirection retains stderr even where a PowerShell transcript cannot capture it.
    # No secret is passed in argv or printed; start-jury owns environment isolation.
    $process = Start-Process -FilePath $powershell -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru `
        -WorkingDirectory (Split-Path -Parent $PSScriptRoot) `
        -RedirectStandardOutput (Join-Path $runPath 'stdout.log') `
        -RedirectStandardError (Join-Path $runPath 'stderr.log')
    Write-Host ("Laptop launcher exit code: {0}. See stdout.log and stderr.log." -f $process.ExitCode)
    if ($process.ExitCode -eq 0) { $exitCode = 0 }
} catch {
    Write-Host ('Laptop launcher failed: ' + $_.Exception.Message)
} finally {
    if ($transcribing) {
        try { $null = Stop-Transcript } catch { }
    }
}
exit $exitCode
