<#
install.ps1 - Set up Quest Hub on Windows 10/11 (x64).

Everything is installed inside this folder: a pinned, checksum-verified
micromamba in .tools\, the Python environment in .mamba-env\ (from
locks\conda-win-64.txt), and the official scrcpy 5.0.1 Windows build (which
bundles a matching adb.exe) in .mamba-env\vendor\scrcpy\. No administrator
rights, no conda/Python/Git installation, and no changes to PATH or the
registry.

Run it by double-clicking install.cmd, or from PowerShell:
  powershell -ExecutionPolicy Bypass -File .\install.ps1 [-Doctor] [-Strict] [-Uninstall [-PurgeData]] [-Yes]

Must stay compatible with Windows PowerShell 5.1 (ships with Windows).
#>
[CmdletBinding()]
param(
    [switch]$Doctor,
    [switch]$Strict,
    [switch]$Uninstall,
    [switch]$PurgeData,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Get-Setting([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($value)) { return $Default }
    return $value
}

$MicromambaVersion = Get-Setting 'QH_MICROMAMBA_VERSION' '2.10.0-0'
$MicromambaSha256 = 'f8b4284bd4eb1134122eaf93b3ea6c7be567e596de5829e1a6f478e34e41974a'   # micromamba-win-64 2.10.0-0
$MicromambaBaseUrl = Get-Setting 'QH_MICROMAMBA_BASE_URL' 'https://github.com/mamba-org/micromamba-releases/releases/download'
$ScrcpyVersion = '5.0.1'
$ScrcpyZipSha256 = 'b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5'     # scrcpy-win64-v5.0.1.zip
$ScrcpyServerSha256 = '764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536'  # scrcpy-server v5.0.1

$ToolsDir = Get-Setting 'QH_TOOLS_DIR' (Join-Path $Root '.tools')
$EnvDir = Get-Setting 'QH_ENV_DIR' (Join-Path $Root '.mamba-env')
$DataDir = Get-Setting 'QH_DATA_DIR' (Join-Path $env:USERPROFILE '.questhub')
$LockFile = Join-Path $Root 'locks\conda-win-64.txt'
$SpecFile = Join-Path $Root 'environment-windows.yml'
$SpecStamp = Join-Path $EnvDir '.questhub-spec.sha256'
$ScrcpyDir = Join-Path $EnvDir 'vendor\scrcpy'
$ScrcpyRequiredFiles = @('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'scrcpy.exe')
$GuiPorts = @(8765, 8443, 8766)

function Log([string]$Message) { Write-Host "[install] $Message" }
function Warn([string]$Message) { Write-Host "[install][WARN] $Message" -ForegroundColor Yellow }
function Die([string]$Message) { Write-Host "[install][ERROR] $Message" -ForegroundColor Red; exit 1 }

function Get-Sha256([string]$Path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }

function Get-TextSha256([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

function Invoke-Download([string]$Url, [string]$Destination) {
    Log "Downloading $Url"
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing -TimeoutSec 600
            return
        } catch {
            if ($attempt -eq 3) { Die "Download failed: $Url ($($_.Exception.Message)). Check the network or the proxy settings." }
            Start-Sleep -Seconds (2 * $attempt)
        }
    }
}

function Confirm-Action([string]$Question) {
    if ($Yes) { return $true }
    $answer = Read-Host "$Question [y/N]"
    return $answer -match '^(y|yes)$'
}

# Refuse to delete anything that is not clearly ours.
function Remove-Safely([string]$Target) {
    if ([string]::IsNullOrWhiteSpace($Target)) { Die 'Refusing to remove an empty path' }
    $full = [IO.Path]::GetFullPath($Target).TrimEnd('\')
    $forbidden = @([IO.Path]::GetFullPath($Root).TrimEnd('\'), [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\'),
                   [IO.Path]::GetPathRoot($full).TrimEnd('\'))
    if ($forbidden -contains $full) { Die "Refusing to remove $full" }
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force
        Log "Removed $full"
    }
}

# The adb server keeps adb.exe locked, and "adb kill-server" returns before the process has exited.
function Stop-BundledAdb {
    $adb = Join-Path $ScrcpyDir 'adb.exe'
    if (-not (Test-Path -LiteralPath $adb)) { return }
    & $adb kill-server 2>$null | Out-Null
    $prefix = [IO.Path]::GetFullPath($ScrcpyDir).TrimEnd('\') + '\'
    for ($i = 0; $i -lt 50; $i++) {
        $running = @(Get-Process -Name adb -ErrorAction SilentlyContinue |
                     Where-Object { $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) })
        if ($running.Count -eq 0) { return }
        if ($i -eq 30) { $running | Stop-Process -Force -ErrorAction SilentlyContinue }   # still there after 3 s
        Start-Sleep -Milliseconds 100
    }
    Warn 'adb.exe is still running; close Quest Hub and try again if removal fails'
}

function New-TempDir {
    $dir = Join-Path ([IO.Path]::GetTempPath()) ("questhub-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $dir | Out-Null
    return $dir
}

# ------------------------------------------------------------------ checks

function Test-Prerequisites {
    if (-not [Environment]::Is64BitOperatingSystem) { Die 'A 64-bit Windows is required.' }
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
    if ($arch -eq 'ARM64') { Warn 'Windows on ARM: the x64 packages run under emulation.' }
    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 17763) { Die "Windows 10 1809 or later is required (build $build)." }
    $drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($Root).Substring(0, 1)) -ErrorAction SilentlyContinue
    if ($drive -and $drive.Free -lt 2GB) { Warn 'Less than 2 GB free on this drive; the installation may fail.' }
    if ($Root.Length -gt 80) { Warn "The folder path is long ($($Root.Length) characters); Windows path limits may break the installation. A short path such as C:\questhub is safer." }
}

# ------------------------------------------------------------------ micromamba

$Mm = $null

function Install-Micromamba {
    $override = Get-Setting 'QH_MICROMAMBA_BIN' ''
    if ($override) {
        if (-not (Test-Path -LiteralPath $override)) { Die "QH_MICROMAMBA_BIN not found: $override" }
        $script:Mm = $override
        return
    }
    $binDir = Join-Path $ToolsDir 'bin'
    $script:Mm = Join-Path $binDir 'micromamba.exe'
    $stamp = Join-Path $binDir 'micromamba.version'
    if ((Test-Path -LiteralPath $Mm) -and (Test-Path -LiteralPath $stamp) -and ((Get-Content -LiteralPath $stamp -Raw).Trim() -eq $MicromambaVersion)) {
        Log "micromamba $MicromambaVersion already installed"
        return
    }
    $tmp = New-TempDir
    try {
        $url = "$MicromambaBaseUrl/$MicromambaVersion/micromamba-win-64"
        $file = Join-Path $tmp 'micromamba.exe'
        Invoke-Download $url $file
        $expected = $MicromambaSha256
        if ($MicromambaVersion -ne '2.10.0-0') {
            # Non-default version: fall back to the checksum published with the release.
            Invoke-Download "$url.sha256" (Join-Path $tmp 'micromamba.sha256')
            $expected = ((Get-Content -LiteralPath (Join-Path $tmp 'micromamba.sha256') -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
        }
        if ((Get-Sha256 $file) -ne $expected) { Die 'micromamba checksum mismatch' }
        New-Item -ItemType Directory -Force -Path $binDir | Out-Null
        Move-Item -Force -LiteralPath $file -Destination $Mm
        Set-Content -LiteralPath $stamp -Value $MicromambaVersion -Encoding ASCII
        Log "Installed micromamba $MicromambaVersion (win-64)"
    } finally {
        Remove-Item -Recurse -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
    }
}

# Run micromamba isolated from any user configuration.
function Invoke-Mm([string[]]$Arguments) {
    $saved = @{ MAMBA_ROOT_PREFIX = $env:MAMBA_ROOT_PREFIX; CONDARC = $env:CONDARC; MAMBARC = $env:MAMBARC }
    $env:MAMBA_ROOT_PREFIX = Join-Path $ToolsDir 'mamba-root'
    $env:CONDARC = ''
    $env:MAMBARC = ''
    try {
        & $Mm --no-rc @Arguments
        if ($LASTEXITCODE -ne 0) { Die "micromamba $($Arguments[0]) failed (exit $LASTEXITCODE)" }
    } finally {
        foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key]) }
    }
}

# ------------------------------------------------------------------ environment

function Get-SpecDigest {
    $text = "micromamba=$MicromambaVersion`n"
    if (Test-Path -LiteralPath $LockFile) { $text += Get-Content -LiteralPath $LockFile -Raw }
    else { $text += Get-Content -LiteralPath $SpecFile -Raw }
    return Get-TextSha256 $text
}

function Install-Environment {
    if ($Strict -and -not (Test-Path -LiteralPath $LockFile)) {
        Die "-Strict requires $LockFile (generate it on a Mac with ./install.sh --relock win-64)"
    }
    $digest = Get-SpecDigest
    $python = Join-Path $EnvDir 'python.exe'
    if ((Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $SpecStamp) -and ((Get-Content -LiteralPath $SpecStamp -Raw).Trim() -eq $digest)) {
        Log "Environment is up to date ($EnvDir)"
        return
    }
    # Rebuild from scratch so removed packages do not linger, but keep the
    # downloaded scrcpy/adb in vendor\ to avoid fetching it again.
    $savedVendor = $null
    $vendor = Join-Path $EnvDir 'vendor'
    if (Test-Path -LiteralPath $vendor) {
        $savedVendor = New-TempDir
        Move-Item -LiteralPath $vendor -Destination (Join-Path $savedVendor 'vendor')
    }
    Remove-Safely $EnvDir
    if (Test-Path -LiteralPath $LockFile) {
        Log "Creating environment from $(Split-Path -Leaf $LockFile)"
        Invoke-Mm @('create', '--yes', '--prefix', $EnvDir, '--file', $LockFile)
    } else {
        Warn 'No lock file for win-64; solving environment-windows.yml (versions may drift)'
        Invoke-Mm @('create', '--yes', '--prefix', $EnvDir, '--file', $SpecFile)
    }
    if ($savedVendor) {
        Move-Item -LiteralPath (Join-Path $savedVendor 'vendor') -Destination $vendor
        Remove-Item -Recurse -Force -LiteralPath $savedVendor -ErrorAction SilentlyContinue
    }
    Set-Content -LiteralPath $SpecStamp -Value $digest -Encoding ASCII
}

function Install-Scrcpy {
    if ((Get-Setting 'QH_SKIP_BINARIES' '0') -eq '1') { Log 'Skipping adb/scrcpy (QH_SKIP_BINARIES=1)'; return }
    $stamp = Join-Path $ScrcpyDir 'questhub.version'
    $server = Join-Path $ScrcpyDir 'scrcpy-server'
    $complete = (Test-Path -LiteralPath $stamp) -and ((Get-Content -LiteralPath $stamp -Raw).Trim() -eq $ScrcpyVersion) -and
        (Test-Path -LiteralPath $server) -and ((Get-Sha256 $server) -eq $ScrcpyServerSha256)
    foreach ($exe in $ScrcpyRequiredFiles) {   # a deleted or quarantined adb.exe must be repaired by a re-run
        if (-not (Test-Path -LiteralPath (Join-Path $ScrcpyDir $exe))) { $complete = $false }
    }
    if ($complete) {
        Log "scrcpy $ScrcpyVersion already installed"
        return
    }
    $tmp = New-TempDir
    try {
        $zip = Join-Path $tmp 'scrcpy.zip'
        Invoke-Download "https://github.com/Genymobile/scrcpy/releases/download/v$ScrcpyVersion/scrcpy-win64-v$ScrcpyVersion.zip" $zip
        if ((Get-Sha256 $zip) -ne $ScrcpyZipSha256) { Die 'scrcpy archive checksum mismatch' }
        Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
        $inner = Get-ChildItem -LiteralPath $tmp -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'scrcpy-server') } | Select-Object -First 1
        if (-not $inner) { Die 'Unexpected scrcpy archive layout' }
        if ((Get-Sha256 (Join-Path $inner.FullName 'scrcpy-server')) -ne $ScrcpyServerSha256) { Die 'scrcpy-server checksum mismatch' }
        Stop-BundledAdb
        Remove-Safely $ScrcpyDir
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ScrcpyDir) | Out-Null
        Move-Item -LiteralPath $inner.FullName -Destination $ScrcpyDir
        Set-Content -LiteralPath $stamp -Value $ScrcpyVersion -Encoding ASCII
        Log "Installed scrcpy $ScrcpyVersion with adb ($ScrcpyDir)"
    } finally {
        Remove-Item -Recurse -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
    }
}

function Initialize-AppData {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataDir 'tls') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $Root 'recordings') | Out-Null
}

# ------------------------------------------------------------------ doctor

$script:Fails = 0
$script:Warns = 0

function Report([string]$Status, [string]$Name, [string]$Detail) {
    $color = 'Gray'
    switch ($Status) {
        'OK' { $color = 'Green' }
        'WARN' { $color = 'Yellow'; $script:Warns++ }
        'FAIL' { $color = 'Red'; $script:Fails++ }
    }
    Write-Host ('  {0,-5} ' -f $Status) -ForegroundColor $color -NoNewline
    Write-Host ('{0,-22} {1}' -f $Name, $Detail)
}

function Test-PortInUse([int]$Port) {
    try {
        return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
    } catch {
        return $false
    }
}

function Find-Browser {
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA) | Where-Object { $_ }
    foreach ($rel in @('Google\Chrome\Application\chrome.exe', 'Microsoft\Edge\Application\msedge.exe')) {
        foreach ($r in $roots) {
            $candidate = Join-Path $r $rel
            if (Test-Path -LiteralPath $candidate) { return $candidate }
        }
    }
    return $null
}

function Invoke-Doctor {
    Write-Host '[install] Doctor'
    $build = [Environment]::OSVersion.Version
    if ($build.Build -ge 17763) { Report 'OK' 'Windows' "$($build.ToString())" } else { Report 'FAIL' 'Windows' "$($build.ToString()) (10 1809+ required)" }
    if (Test-Path -LiteralPath (Join-Path $ToolsDir 'bin\micromamba.exe')) { Report 'OK' 'micromamba' $MicromambaVersion } else { Report 'FAIL' 'micromamba' 'missing (run install.cmd)' }
    $python = Join-Path $EnvDir 'python.exe'
    if (Test-Path -LiteralPath $python) {
        $version = (& $python -c "import sys; print(sys.version.split()[0])") 2>$null
        Report 'OK' 'python' "$version ($python)"
        & $python -c "import av, numpy, fastapi, uvicorn, cryptography, qrcode" 2>$null
        if ($LASTEXITCODE -eq 0) { Report 'OK' 'GUI packages' 'av numpy fastapi uvicorn cryptography qrcode' } else { Report 'FAIL' 'GUI packages' 'import failed (re-run install.cmd)' }
    } else {
        Report 'FAIL' 'python' 'missing (run install.cmd)'
    }
    $adb = Join-Path $ScrcpyDir 'adb.exe'
    if (Test-Path -LiteralPath $adb) {
        $line = (& $adb version 2>$null | Select-Object -First 1)
        Report 'OK' 'adb' "$line"
    } else { Report 'FAIL' 'adb' 'missing (run install.cmd)' }
    $server = Join-Path $ScrcpyDir 'scrcpy-server'
    if ((Test-Path -LiteralPath $server) -and ((Get-Sha256 $server) -eq $ScrcpyServerSha256)) { Report 'OK' 'scrcpy-server' $ScrcpyVersion }
    else { Report 'FAIL' 'scrcpy-server' "missing or not $ScrcpyVersion" }
    if (Test-Path -LiteralPath (Join-Path $ScrcpyDir 'scrcpy.exe')) { Report 'OK' 'scrcpy (native view)' $ScrcpyVersion } else { Report 'WARN' 'scrcpy (native view)' 'missing' }
    $browser = Find-Browser
    if ($browser) { Report 'OK' 'browser' $browser } else { Report 'WARN' 'browser' 'Chrome / Edge not found (the GUI needs WebCodecs)' }
    foreach ($port in $GuiPorts) {
        if (Test-PortInUse $port) { Report 'WARN' "port $port" 'in use (another Quest Hub or app?)' } else { Report 'OK' "port $port" 'free' }
    }
    Report 'INFO' 'firewall' 'Allow python.exe on private networks when Windows asks (needed for the viewer and adb over Wi-Fi)'
    Write-Host ("[install] Doctor: {0} failure(s), {1} warning(s)" -f $script:Fails, $script:Warns)
    if ($script:Fails -gt 0) { exit 1 }
}

# ------------------------------------------------------------------ uninstall

function Invoke-Uninstall {
    $what = "$ToolsDir and $EnvDir"
    if ($PurgeData) { $what += " and $DataDir (registry, certificates, settings)" }
    if (-not (Confirm-Action "Remove $what?")) { Log 'Cancelled'; return }
    Stop-BundledAdb
    Remove-Safely $EnvDir
    Remove-Safely $ToolsDir
    if ($PurgeData) { Remove-Safely $DataDir }
    Log 'Uninstalled'
}

# ------------------------------------------------------------------ main

if ($Uninstall) { Invoke-Uninstall; exit 0 }
if ($Doctor) { Invoke-Doctor; exit 0 }

Test-Prerequisites
Install-Micromamba
Install-Environment
Install-Scrcpy
Initialize-AppData
Invoke-Doctor
Log 'Done.'
Log '  GUI: double-click questhub.cmd (or run .\questhub.cmd)'
exit 0   # do not leak the exit code of the last external command (adb, python)
