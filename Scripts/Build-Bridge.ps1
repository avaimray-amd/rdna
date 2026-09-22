[CmdletBinding()]
param([ValidateRange(1,64)][int]$Jobs=2)
. "$PSScriptRoot/Common.ps1"
Assert-Pins
Import-MSVC
$build = Join-Path $WorkspaceRoot 'PyGpuDirect/build-laptop'
$install = Join-Path $WorkspaceRoot 'PyGpuDirect/install'
Invoke-Checked $CMake @('-S',"$WorkspaceRoot/PyGpuDirect",'-B',$build,'-G','Ninja',
    '-DCMAKE_BUILD_TYPE=Release',"-DCMAKE_MAKE_PROGRAM=$Ninja","-DCMAKE_INSTALL_PREFIX=$install")
Invoke-Checked $CMake @('--build',$build,'--target','tcore_backend','--parallel',"$Jobs")
Invoke-Checked $CMake @('--install',$build)
if (-not (Test-Path -LiteralPath "$install/bin/tcore_backend.dll")) { throw 'Native bridge was not installed' }
Write-Output 'Bridge installed. Never copy build-laptop/native/tcore2/csimulate_shared.dll: it is a link stub, not the simulator.'