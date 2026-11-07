# Velune CLI installer for Windows (PowerShell 5.1+ / 7+).
#
#   irm https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/scripts/install.ps1 | iex
#
# Installs Velune into its own isolated environment with uv
# (https://docs.astral.sh/uv/), so it never fights the packages of any other
# tool and works even with no Python installed — uv downloads a private one.
# Re-run the same command to upgrade.
#
# Environment overrides:
#   VELUNE_PACKAGE         what to install (default: velune-cli; may be a version
#                          spec like "velune-cli==0.9.9" or a local wheel path)
#   VELUNE_PYTHON          Python version for the environment (default: 3.12)
#   VELUNE_NO_MODIFY_PATH  set to 1 to leave the user PATH untouched

# Before downloading anything the installer checks the CPU architecture and
# stops with an explanation on a platform Velune does not support (see
# "Platform support" in the README).

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

# Everything runs inside a function: under `irm | iex` a top-level `exit`
# would close the user's terminal, so failures `throw` instead.
function Install-Velune {
    $ErrorActionPreference = 'Stop'

    # $IsWindows only exists in PowerShell 7+; Windows PowerShell 5.1 is Windows-only.
    $onWindows = if ($PSVersionTable.PSEdition -eq 'Core') { [bool]$IsWindows } else { $true }
    $reason = Get-VeluneUnsupportedPlatformReason -Architecture (Get-VeluneHostArchitecture) -IsWindowsOs $onWindows
    if ($reason) {
        throw ("$reason.`nSupported platforms and manual install options: " +
            "https://github.com/Surya-Hariharan/Velune-CLI#platform-support`n" +
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

    Write-Host "==> Installing $package (isolated environment, Python $pythonVersion)..."
    & $uv tool install --force --upgrade --python $pythonVersion $package
    if ($LASTEXITCODE -ne 0) { throw "installing $package failed (output above)" }

    $binDir = (& $uv tool dir --bin).Trim()
    & (Join-Path $binDir 'velune.exe') --version
    if ($LASTEXITCODE -ne 0) { throw 'velune was installed but does not start' }

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
