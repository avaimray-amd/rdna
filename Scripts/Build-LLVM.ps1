[CmdletBinding()]
param([ValidateRange(1,64)][int]$Jobs=2)
. "$PSScriptRoot/Common.ps1"
Assert-Pins
Import-MSVC
$build = Join-Path $WorkspaceRoot 'llvm-project/build-laptop'
Invoke-Checked $CMake @('-S',"$WorkspaceRoot/llvm-project/llvm",'-B',$build,'-G','Ninja',
    '-DCMAKE_BUILD_TYPE=Release',"-DCMAKE_MAKE_PROGRAM=$Ninja",'-DLLVM_ENABLE_PROJECTS=clang;lld',
    '-DLLVM_TARGETS_TO_BUILD=AMDGPU;X86','-DLLVM_ENABLE_ASSERTIONS=OFF','-DLLVM_INCLUDE_TESTS=OFF',
    '-DLLVM_INCLUDE_BENCHMARKS=OFF','-DLLVM_INCLUDE_EXAMPLES=OFF','-DLLVM_ENABLE_ZLIB=OFF','-DLLVM_ENABLE_ZSTD=OFF')
Invoke-Checked $CMake @('--build',$build,'--target','clang','lld','llvm-objdump','--parallel',"$Jobs")
Invoke-Checked "$build/bin/clang.exe" @('--version')