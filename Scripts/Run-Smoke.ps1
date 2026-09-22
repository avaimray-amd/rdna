[CmdletBinding()]
param(
    [string]$ModelPackage,
    [string]$CompilerBin,
    [string]$BridgeBin,
    [string]$OutputDirectory,
    [string]$PythonExecutable
)
. "$PSScriptRoot/Common.ps1"
Assert-Pins
if ($PythonExecutable) { $Python = (Resolve-Path -LiteralPath $PythonExecutable).Path }
if (-not $ModelPackage) { $ModelPackage = "$ModelSource/build-laptop/package" }
if (-not $CompilerBin) { $CompilerBin = "$WorkspaceRoot/llvm-project/build-laptop/bin" }
if (-not $BridgeBin) { $BridgeBin = "$WorkspaceRoot/PyGpuDirect/install/bin" }
if (-not $OutputDirectory) { $OutputDirectory = "$WorkspaceRoot/output/smoke-$(Get-Date -Format yyyyMMdd-HHmmss)" }
$ModelPackage = [IO.Path]::GetFullPath($ModelPackage)
$CompilerBin = [IO.Path]::GetFullPath($CompilerBin)
$BridgeBin = [IO.Path]::GetFullPath($BridgeBin)
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$env:CLANG_EXE = Join-Path $CompilerBin 'clang.exe'
$env:OBJDUMP_EXE = Join-Path $CompilerBin 'llvm-objdump.exe'
$env:LINKER_EXE = Join-Path $CompilerBin 'lld.exe'
$env:PYTHONPATH = "$WorkspaceRoot/Tracing;$WorkspaceRoot/PyGpuDirect/src;$WorkspaceRoot/GpuKernelLab"
$env:PYTHONUNBUFFERED = '1'
$env:PATH = "$BridgeBin;$env:PATH"
foreach ($path in @($Python,$env:CLANG_EXE,$env:OBJDUMP_EXE,$env:LINKER_EXE,"$BridgeBin/tcore_backend.dll","$ModelPackage/bin/csimulate_shared.dll")) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing prerequisite: $path" }
}
Push-Location "$WorkspaceRoot/GpuKernelLab"
try {
    Invoke-Checked $Python @('Experiments/HIP/Rasteriser/run_pipeline_model.py',
        '--model-package',$ModelPackage,'--output-dir',$OutputDirectory,'--width','128','--height','128','--num-triangles','8')
} finally { Pop-Location }
$result = Get-Content -LiteralPath (Join-Path $OutputDirectory 'result.json') -Raw | ConvertFrom-Json
foreach ($field in @('reuse_matches','connectivity_matches','vertex_matches','coverage_matches_cpu','fragment_matches','queues_drained')) {
    if ($result.$field -ne $true) { throw "Validation failed: $field" }
}
if ($result.covered_pixels -ne 24 -or $result.overflow -ne 0 -or $result.width -ne 128 -or $result.height -ne 128 -or $result.triangles -ne 8) {
    throw 'Unexpected smoke-test result'
}
$trace = Join-Path $OutputDirectory 'HIP_RasteriserPipeline.pftrace'
if (-not (Test-Path -LiteralPath $trace) -or (Get-Item -LiteralPath $trace).Length -eq 0) { throw 'Perfetto trace was not generated' }
Write-Output "PASS: 128x128, 8 triangles, 24 covered pixels. Perfetto trace: $trace"