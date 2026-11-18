# Velune CLI installer for Windows (PowerShell 5.1+ / 7+).
#
#   irm https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/scripts/install.ps1 | iex
#
# Installs Velune into its own isolated environment with uv
# (https://docs.astral.sh/uv/), so it never fights the packages of any other
# tool and works even with no Python installed — uv downloads a private one.
# Re-run the same command to upgrade. Close Velune first: replacing a running
# installation on Windows can destroy it, so the installer refuses while any
# Velune process is still running from it.
#
# Environment overrides:
#   VELUNE_PACKAGE         what to install (default: velune-cli; may be a version
#                          spec like "velune-cli==0.9.9" or a local wheel path)
#   VELUNE_PYTHON          Python version for the environment (default: 3.12)
#   VELUNE_NO_MODIFY_PATH  set to 1 to leave the user PATH untouched
#   VELUNE_STOP_RUNNING    set to 1 to stop running Velune processes instead of
#                          refusing (unsaved work in them is lost)

# Before downloading anything the installer checks the CPU architecture and
# stops with an explanation on a platform Velune does not support (see
# "Platform support" in docs/reference.md).

# Returns why the platform is unsupported, or $null when it is supported.
# Architecture is a .NET RuntimeInformation name (X64, Arm64, X86, ...) or a
# PROCESSOR_ARCHITECTURE value (AMD64, ARM64, x86, ...).
function Get-VeluneUnsupportedPlatformReason {
    param(
        [string]$Architecture,
        [bool]$IsWindowsOs = $true
    )
    if (-not $IsWindowsOs) {
        return 'this is not Windows; on macOS / Linux / WSL use scripts/install.sh instead'
    }
    switch -Regex ($Architecture) {
        '^(x64|amd64)$' { return $null }
        '^(arm64|aarch64)$' {
            return 'Windows on ARM64 is not supported: required dependencies (cryptography, tiktoken) publish no Windows ARM64 wheels'
        }
        default { return "unsupported CPU architecture: $Architecture" }
    }
}

# The architecture of the operating system, not of this PowerShell process: a
# 32-bit or x64-emulated shell on an ARM64 machine still reports the OS value.
function Get-VeluneHostArchitecture {
    try {
        $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        if ($arch) { return $arch }
    } catch { }
    if ($env:PROCESSOR_ARCHITEW6432) { return $env:PROCESSOR_ARCHITEW6432 }
    return $env:PROCESSOR_ARCHITECTURE
}

# Processes that hold an existing Velune installation open: anything running from
# the uv tool environment, or the `velune.exe` launcher in uv's bin directory.
# Windows locks these files, and uv fails part-way through replacing them, which
# leaves a broken or half-deleted installation ("Velune disappeared").
# $Processes is injectable for tests; each item needs .Id and .Path.
function Get-VeluneRunningProcesses {
    param(
        [string]$ToolDir,
        [string]$BinDir,
        $Processes = $null
    )
    if ($null -eq $Processes) {
        $Processes = Get-Process -ErrorAction SilentlyContinue
    }
    $envDir = if ($ToolDir) { (Join-Path $ToolDir 'velune-cli').TrimEnd('\') + '\' } else { $null }
    $launcher = if ($BinDir) { Join-Path $BinDir 'velune.exe' } else { $null }
    $found = @()
    foreach ($p in $Processes) {
        $path = $p.Path
        if (-not $path) { continue }
        if (($envDir -and $path.StartsWith($envDir, [System.StringComparison]::OrdinalIgnoreCase)) -or
            ($launcher -and [string]::Equals($path, $launcher, [System.StringComparison]::OrdinalIgnoreCase))) {
            $found += $p
        }
    }
    return ,$found
}

# Everything runs inside a function: under `irm | iex` a top-level `exit`
# would close the user's terminal, so failures `throw` instead.
function Install-Velune {
    $ErrorActionPreference = 'Stop'

    # $IsWindows only exists in PowerShell 7+; Windows PowerShell 5.1 is Windows-only.
    $onWindows = if ($PSVersionTable.PSEdition -eq 'Core') { [bool]$IsWindows } else { $true }
    $reason = Get-VeluneUnsupportedPlatformReason -Architecture (Get-VeluneHostArchitecture) -IsWindowsOs $onWindows
    if ($reason) {
        throw ("$reason.`nSupported platforms and manual install options: " +
            "https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/reference.md#platform-support`n" +
            'Nothing was downloaded or installed.')
    }

    $package = if ($env:VELUNE_PACKAGE) { $env:VELUNE_PACKAGE } else { 'velune-cli' }
    $pythonVersion = if ($env:VELUNE_PYTHON) { $env:VELUNE_PYTHON } else { '3.12' }

    function Find-Uv {
        $cmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        foreach ($candidate in @(
                (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
                (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'))) {
            if (Test-Path $candidate) { return $candidate }
        }
        return $null
    }

    $uv = Find-Uv
    if (-not $uv) {
        Write-Host '==> Installing uv (isolated Python package manager)...'
        $env:UV_NO_MODIFY_PATH = '1'
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression | Out-Null
        $uv = Find-Uv
        if (-not $uv) {
            throw 'uv installation failed; see https://docs.astral.sh/uv/getting-started/installation/'
        }
    }

    # Never replace a running installation (see Get-VeluneRunningProcesses).
    $binDir = (& $uv tool dir --bin).Trim()
    $running = Get-VeluneRunningProcesses -ToolDir (& $uv tool dir).Trim() -BinDir $binDir
    if ($running.Count -gt 0) {
        $ids = ($running | ForEach-Object { $_.Id }) -join ', '
        if ($env:VELUNE_STOP_RUNNING -ne '1') {
            throw ("Velune is still running (PID $ids). Updating a running Velune on Windows " +
                "can delete its files part-way. Close every Velune window, then run the " +
                "installer again (or set VELUNE_STOP_RUNNING=1 to stop them for you).`n" +
                'Nothing was changed.')
        }
        Write-Host "==> Stopping running Velune (PID $ids)..."
        $running | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    # One extra forced install repairs a half-written environment (e.g. a file
    # locked by antivirus); a second failure is reported.
    $velune = Join-Path $binDir 'velune.exe'
    foreach ($attempt in 1, 2) {
        Write-Host "==> Installing $package (isolated environment, Python $pythonVersion)..."
        & $uv tool install --force --upgrade --python $pythonVersion $package
        if ($LASTEXITCODE -ne 0) { throw "installing $package failed (output above)" }
        & $velune --version
        if ($LASTEXITCODE -eq 0) { break }
        if ($attempt -eq 2) {
            throw "velune was installed but does not start. Close every Velune window and run: uv tool install --force $package"
        }
        Write-Host '==> velune did not start; reinstalling once to repair it...'
    }

    if ($env:VELUNE_NO_MODIFY_PATH -ne '1') {
        & $uv tool update-shell *> $null
    }

    $onPath = ($env:Path -split ';') -contains $binDir
    if ($onPath) {
        Write-Host '==> Done. Run: velune'
    } else {
        Write-Host '==> Done. Open a new terminal, then run: velune'
    }
}

# Tests dot-source this file to exercise the functions above without installing.
if ($env:VELUNE_INSTALLER_SOURCE_ONLY -ne '1') {
    Install-Velune
}
