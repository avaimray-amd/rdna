[CmdletBinding()]
param([ValidateRange(1,64)][int]$Jobs=2, [switch]$ConfigureOnly)
. "$PSScriptRoot/Common.ps1"
if (-not (Test-Path -LiteralPath "$ModelSource/.migration-archive.sha256")) { throw 'Run Initialize-Source.ps1 first.' }
$build = Join-Path $ModelSource 'build-laptop'
$package = Join-Path $build 'package'
[IO.Directory]::CreateDirectory($build) | Out-Null
$lock = Get-Content -LiteralPath "$WorkspaceRoot/Build/magnus-conan.lock" -Raw | ConvertFrom-Json
if ($lock.profile_host -notmatch 'tools.build:jobs=16') { throw 'Unexpected locked profile; review before changing build concurrency.' }
$lock.profile_host = $lock.profile_host.Replace('tools.build:jobs=16', "tools.build:jobs=$Jobs")
$localLock = Join-Path $build 'laptop-conan.lock'
[IO.File]::WriteAllText($localLock, ($lock | ConvertTo-Json -Depth 30), (New-Object System.Text.UTF8Encoding($false)))
$env:CONAN_NON_INTERACTIVE = '1'
$env:CMAKE_BUILD_PARALLEL_LEVEL = "$Jobs"
Invoke-Checked $Conan @('install',$ModelSource,'--install-folder',$build,
    '--lockfile',$localLock,'--build=missing')
$activation = Join-Path $build 'activate.ps1'
if (-not (Test-Path -LiteralPath $activation)) { throw 'Conan did not generate the expected build environment.' }
. $activation
$cmakeFromConan = (Get-Command cmake.exe -ErrorAction Stop).Source
Invoke-Checked $cmakeFromConan @('-S',$ModelSource,'-B',$build,'-G','Ninja',
    "-DCMAKE_TOOLCHAIN_FILE=$build/conan_toolchain.cmake",'-DCMAKE_BUILD_TYPE=Release',
    "-DCMAKE_INSTALL_PREFIX=$package",'-DGC_BUILD_MODEL=TB_ONE_MODEL',"-DPYTHON=$Python","-DPYTHON3=$Python")
if ($ConfigureOnly) { Write-Output 'Magnus configured only; no model has been built.'; return }
Invoke-Checked $cmakeFromConan @('--build',$build,'--parallel',"$Jobs")
Invoke-Checked $cmakeFromConan @('--install',$build)
if (-not (Test-Path -LiteralPath "$package/bin/csimulate_shared.dll") -or -not (Test-Path -LiteralPath "$package/runtime")) {
    throw 'Magnus runtime installation is incomplete'
}
Write-Output "Magnus runtime ready: $package"