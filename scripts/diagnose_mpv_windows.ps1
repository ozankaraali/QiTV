# TEMPORARY: independent native exception capture for the Windows source smoke.
# The smoke still launches/owns mpv.exe directly and decides its exit verdict.
# ProcDump uses a PSS clone to minimize time spent suspending the crashing process.
# https://learn.microsoft.com/en-us/sysinternals/downloads/procdump
# https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/debugger-download-tools
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Prepare', 'Capture', 'Analyze')]
    [string]$Mode
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $IsWindows -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'This temporary diagnostic is restricted to the owned Windows CI runner.'
}
$root = Split-Path $PSScriptRoot -Parent
$evidence = Join-Path $root 'build/mpv-smoke/windows-native'
$tools = Join-Path $env:RUNNER_TEMP 'qitv-native-debuggers'
$capture = Join-Path $tools 'private-capture'
$procDump = Join-Path $tools 'procdump64.exe'
$cdb = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits/10/Debuggers/x64/cdb.exe'
New-Item -ItemType Directory -Force $evidence | Out-Null
# Never publish raw dumps: inherited process memory can contain CI credentials.
New-Item -ItemType Directory -Force $capture | Out-Null

function Assert-MicrosoftSignature([string]$Path) {
    $signature = Get-AuthenticodeSignature -FilePath $Path
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation(?:,|$)') {
        throw "Not a valid Microsoft-signed diagnostic tool: $Path ($($signature.Status))"
    }
}

if ($Mode -eq 'Prepare') {
    New-Item -ItemType Directory -Force $tools | Out-Null
    $archive = Join-Path $tools 'Procdump.zip'
    Invoke-WebRequest 'https://download.sysinternals.com/files/Procdump.zip' -OutFile $archive -TimeoutSec 60
    Expand-Archive -Path $archive -DestinationPath $tools -Force
    Assert-MicrosoftSignature $procDump
    if (-not (Test-Path $cdb)) {
        # Windows SDK 10.0.22621.5040: install ONLY the debugging tools, not a toolchain.
        # https://learn.microsoft.com/en-us/windows/apps/windows-sdk/downloads
        $installer = Join-Path $tools 'winsdksetup.exe'
        Invoke-WebRequest 'https://go.microsoft.com/fwlink/?linkid=2311806' -OutFile $installer -TimeoutSec 60
        Assert-MicrosoftSignature $installer
        $install = Start-Process -FilePath $installer -ArgumentList @(
            '/quiet', '/norestart', '/features', 'OptionId.WindowsDesktopDebuggers',
            '/log', "`"$(Join-Path $evidence 'sdk-install.log')`""
        ) -PassThru
        if (-not $install.WaitForExit(480000)) {
            $install.Kill($true)
            throw 'Windows debugging tools installation exceeded 480 seconds'
        }
        if ($install.ExitCode -notin @(0, 3010)) {
            throw "Windows debugging tools installation exited $($install.ExitCode)"
        }
    }
    Assert-MicrosoftSignature $cdb
    @($procDump, $cdb) | ForEach-Object {
        $item = Get-Item $_
        [ordered]@{
            path = $item.FullName
            version = $item.VersionInfo.FileVersion
            sha256 = (Get-FileHash $_ -Algorithm SHA256).Hash
        }
    } | ConvertTo-Json | Set-Content (Join-Path $evidence 'tools.json')
    exit 0
}

if ($Mode -eq 'Capture') {
    Assert-MicrosoftSignature $procDump
    if (@(Get-Process | Where-Object ProcessName -eq 'mpv').Count -gt 0) {
        throw 'An MPV process already exists; refusing ambiguous name-based attachment'
    }
    if (Get-ChildItem $capture -Filter '*.dmp') {
        throw 'Crash evidence already exists; refusing to mix diagnostic runs'
    }
    Copy-Item (Join-Path $root 'native/mpv/bin/mpv.exe') $capture
    Copy-Item (Join-Path $root 'native/mpv/bundle.json') $evidence
    Get-FileHash (Join-Path $capture 'mpv.exe') -Algorithm SHA256 |
        Select-Object -Property @('Hash', 'Path') | ConvertTo-Json | Set-Content (Join-Path $evidence 'image.json')
    $logPath = Join-Path $evidence 'procdump.stdout.log'
    $errorPath = Join-Path $evidence 'procdump.stderr.log'
    $observer = [System.Diagnostics.Process]::new()
    $observer.StartInfo.FileName = $procDump
    $observer.StartInfo.UseShellExecute = $false
    $observer.StartInfo.RedirectStandardOutput = $true
    $observer.StartInfo.RedirectStandardError = $true
    foreach ($argument in @('-accepteula', '-mm', '-e', '-r', '-n', '1', '-w', 'mpv.exe', $capture)) {
        $observer.StartInfo.ArgumentList.Add($argument)
    }
    $log = [System.IO.StreamWriter]::new($logPath, $false)
    $log.AutoFlush = $true
    $outputRead = $null
    $errorRead = $null
    $smokeExit = $null
    $started = $false
    try {
        if (-not $observer.Start()) { throw 'Could not start ProcDump' }
        $started = $true
        $errorRead = $observer.StandardError.ReadToEndAsync()
        # Wait for the observer's real readiness message, never an arbitrary sleep.
        $readyDeadline = [System.Diagnostics.Stopwatch]::StartNew()
        while ($true) {
            $remaining = 10000 - [int]$readyDeadline.ElapsedMilliseconds
            $lineRead = $observer.StandardOutput.ReadLineAsync()
            if ($remaining -le 0 -or -not $lineRead.Wait($remaining)) {
                throw 'ProcDump did not become ready within 10 seconds'
            }
            $line = $lineRead.GetAwaiter().GetResult()
            if ($null -eq $line) { throw 'ProcDump exited before waiting for mpv.exe' }
            $log.WriteLine($line)
            if ($line -match 'Waiting for process') { break }
        }
        $outputRead = $observer.StandardOutput.ReadToEndAsync()
        # Keep the original smoke command and all production deadlines unchanged.
        # Preserve its exit code even when diagnostics subsequently fail.
        $PSNativeCommandUseErrorActionPreference = $false
        & uv run --frozen --no-sync python (Join-Path $root 'scripts/smoke_mpv.py') `
            --fixture (Join-Path $root 'tests/fixtures/mpv-smoke.mp4') `
            --report (Join-Path $root 'build/mpv-smoke/source.json')
        $smokeExit = $LASTEXITCODE
        if (-not $observer.WaitForExit(5000)) {
            throw 'ProcDump did not finish within 5 seconds after the smoke exited'
        }
        $observerExit = $observer.ExitCode
        $report = Get-Content (Join-Path $root 'build/mpv-smoke/source.json') -Raw | ConvertFrom-Json
        $dumps = @(Get-ChildItem $capture -Filter '*.dmp')
        [ordered]@{
            smoke_exit = $smokeExit
            observer_exit = $observerExit
            native_exits = $report.native_exits
            dumps = @($dumps | ForEach-Object Name)
        } | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $evidence 'capture.json')
        if ($observerExit -ne 0) { throw "ProcDump exited $observerExit" }
        if (@($report.native_exits | Where-Object { $_.exit_status -eq 'CrashExit' }).Count -gt 0 -and $dumps.Count -eq 0) {
            throw 'Native smoke crashed but ProcDump captured no dump'
        }
    } finally {
        if ($started -and -not $observer.HasExited) {
            # This is diagnostic cleanup, not a replacement for smoke child ownership.
            $observer.Kill($true)
            if (-not $observer.WaitForExit(5000)) {
                throw 'ProcDump could not be reaped within 5 seconds'
            }
        }
        if ($null -ne $outputRead) { $log.Write($outputRead.GetAwaiter().GetResult()) }
        if ($null -ne $errorRead) { $errorRead.GetAwaiter().GetResult() | Set-Content $errorPath }
        $log.Dispose()
        $observer.Dispose()
    }
    exit $smokeExit
}

# Offline analysis cannot affect the live smoke's deadlines or native exit status.
$dumps = @(Get-ChildItem $capture -Filter '*.dmp')
if ($dumps.Count -eq 0) {
    Write-Host 'No Windows native dump was captured; inspect source.json and ProcDump logs.'
    exit 0
}
Assert-MicrosoftSignature $cdb
foreach ($dump in $dumps) {
    $stack = Join-Path $evidence "$($dump.BaseName).stack.log"
    $errors = Join-Path $evidence "$($dump.BaseName).cdb.stderr.log"
    $symbols = "srv*$(Join-Path $tools 'symbols')*https://msdl.microsoft.com/download/symbols"
    $commands = '.exr -1; .ecxr; r; u @rip-20 L40; kv; ~* kv; lmv; !analyze -v; q'
    $analysis = Start-Process -FilePath $cdb -ArgumentList @(
        '-z', "`"$($dump.FullName)`"", '-y', "`"$symbols`"", '-i', "`"$capture`"",
        '-c', "`"$commands`""
    ) -RedirectStandardOutput $stack -RedirectStandardError $errors -PassThru
    if (-not $analysis.WaitForExit(120000)) {
        $analysis.Kill($true)
        throw 'CDB dump analysis exceeded 120 seconds; partial text diagnostics are retained'
    }
    if ($analysis.ExitCode -ne 0) { throw "CDB dump analysis exited $($analysis.ExitCode)" }
    Get-Content $stack
}
