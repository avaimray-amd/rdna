$ErrorActionPreference = 'Stop'
$WorkspaceRoot = Split-Path -Parent $PSScriptRoot
$ModelSource = Join-Path $WorkspaceRoot 'one_model-magnus-13.0.8982267.21415-src'
$Python = Join-Path $WorkspaceRoot '.venv/Scripts/python.exe'
$Conan = Join-Path $WorkspaceRoot '.conanenv/Scripts/conan.exe'
$CMake = Join-Path $WorkspaceRoot '.venv/Scripts/cmake.exe'
$Ninja = Join-Path $WorkspaceRoot '.venv/Scripts/ninja.exe'

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE. Stop at this step." }
}

function Get-SafePath([string]$Base, [string]$Relative) {
    $basePath = [IO.Path]::GetFullPath($Base).TrimEnd('\') + '\'
    $path = [IO.Path]::GetFullPath((Join-Path $Base $Relative))
    if (-not $path.StartsWith($basePath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes base directory: $Relative"
    }
    return $path
}

function Assert-Hash([string]$Path, [string]$Expected) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing file: $Path" }
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Expected) {
        throw "SHA-256 mismatch: $Path"
    }
}

function Assert-Pins {
    $snapshot = Get-Content -LiteralPath (Join-Path $WorkspaceRoot 'snapshot.json') -Raw | ConvertFrom-Json
    foreach ($repository in $snapshot.repositories) {
        $path = Get-SafePath $WorkspaceRoot $repository.path
        if (-not (Test-Path -LiteralPath (Join-Path $path '.git'))) { throw "Missing submodule: $($repository.path)" }
        $head = git --no-optional-locks -C $path rev-parse HEAD
        if ($LASTEXITCODE -ne 0 -or $head -ne $repository.commit) { throw "Unexpected HEAD: $($repository.path)" }
    }
}

function Import-MSVC {
    if (Get-Command cl.exe -ErrorAction SilentlyContinue) { return }
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
    if (-not (Test-Path -LiteralPath $vswhere)) { throw 'Install Visual Studio 2022 C++ Build Tools and a Windows SDK, then reopen PowerShell.' }
    $installation = & $vswhere -latest -products '*' -version '[17.0,18.0)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $installation) { throw 'Visual Studio 2022 C++ tools were not found. Ask IT to install the Desktop development with C++ workload.' }
    $vcvars = Join-Path $installation 'VC/Auxiliary/Build/vcvars64.bat'
    $environment = & cmd.exe /d /s /c "`"$vcvars`" >nul && set"
    if ($LASTEXITCODE -ne 0) { throw 'MSVC environment setup failed' }
    foreach ($line in $environment) {
        if ($line -match '^([^=]+)=(.*)$') { Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2] }
    }
}