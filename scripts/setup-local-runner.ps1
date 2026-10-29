param(
    [string]$Repository = 'Polozhentsev-Ivan/DjedAI',
    [string]$Distribution = 'DjedAI-CI',
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'DjedAI-CI'),
    [string]$RunnerName = "$env:COMPUTERNAME-djedai"
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
if ($Repository -notmatch '^[\w.-]+/[\w.-]+$' -or $Distribution -notmatch '^[\w.-]+$' -or $RunnerName -notmatch '^[\w.-]+$') {
    throw 'Invalid repository, distribution or runner name.'
}
function Assert-Exit([string]$Operation) {
    if ($LASTEXITCODE -ne 0) { throw "$Operation failed (exit $LASTEXITCODE)." }
}
function Invoke-Native([string]$Command, [string[]]$Arguments, [string]$StandardInput) {
    # Windows PowerShell 5.1 otherwise treats normal native stderr progress as fatal.
    $ErrorActionPreference = 'Continue'
    if ($PSBoundParameters.ContainsKey('StandardInput')) {
        $StandardInput | & $Command @Arguments 2>&1 | ForEach-Object { Write-Host "$_" }
    } else {
        & $Command @Arguments 2>&1 | ForEach-Object { Write-Host "$_" }
    }
    Assert-Exit $Command
}

& gh api "repos/$Repository" --jq '.permissions.admin' | Out-Null
Assert-Exit 'GitHub access check'
$packages = & gh api "repos/$Repository/actions/runners/downloads" | ConvertFrom-Json
Assert-Exit 'Runner package lookup'
$package = $packages | Where-Object { $_.os -eq 'linux' -and $_.architecture -eq 'x64' } | Select-Object -First 1
if (!$package -or $package.sha256_checksum -notmatch '^[a-f0-9]{64}$') { throw 'Missing verified Linux x64 runner package.' }

$distributions = (& wsl.exe --list --quiet) -replace "`0", ''
Assert-Exit 'WSL discovery'
if ($Distribution -notin ($distributions | ForEach-Object { $_.Trim() })) {
    New-Item -ItemType Directory -Force -Path $InstallDirectory | Out-Null
    $archive = Join-Path $InstallDirectory 'bootstrap.tar'
    $container = "djedai-ci-bootstrap-$([guid]::NewGuid().ToString('N'))"
    Invoke-Native docker @('pull', 'ubuntu:24.04')
    Invoke-Native docker @('create', '--name', $container, 'ubuntu:24.04')
    try {
        Invoke-Native docker @('export', '--output', $archive, $container)
        Invoke-Native wsl.exe @('--import', $Distribution, (Join-Path $InstallDirectory 'linux'), $archive, '--version', '2')
    } finally {
        Invoke-Native docker @('rm', $container)
        if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive }
    }
}

$bootstrap = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'setup-local-runner.sh') -Raw
Invoke-Native wsl.exe @('-d', $Distribution, '-u', 'root', '--exec', 'bash', '-s', '--', $package.download_url, $package.sha256_checksum) $bootstrap
Invoke-Native wsl.exe @('--terminate', $Distribution)

# Registration tokens are short lived and go through stdin, never into a file or command line.
$registration = & gh api --method POST "repos/$Repository/actions/runners/registration-token" | ConvertFrom-Json
Assert-Exit 'Runner registration token request'
try {
    Invoke-Native wsl.exe @('-d', $Distribution, '-u', 'root', '--exec', '/usr/local/sbin/djedai-ci-register', "https://github.com/$Repository", $RunnerName) $registration.token
} finally {
    $registration = $null
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wsl.exe" -Argument "-d $Distribution -u root --exec /usr/local/sbin/djedai-ci-keepalive"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'DjedAI CI Runner' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Keep the isolated DjedAI GitHub Actions runner running in WSL.' -Force | Out-Null
Start-ScheduledTask -TaskName 'DjedAI CI Runner'
Invoke-Native wsl.exe @('-d', $Distribution, '-u', 'root', '--exec', 'systemctl', '--no-pager', 'status', 'docker', 'djedai-ci')
Write-Host "Runner $RunnerName installed. GitHub: https://github.com/$Repository/settings/actions/runners"
