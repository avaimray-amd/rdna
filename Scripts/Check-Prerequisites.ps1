[CmdletBinding()]
param()
. "$PSScriptRoot/Common.ps1"
if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
    throw 'Use 64-bit Windows PowerShell on Windows x64.'
}
foreach ($command in @('git.exe','tar.exe','py.exe')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "Missing prerequisite: $command" }
}
Invoke-Checked py @('-3.12','--version')
Import-MSVC
if (-not (Get-Command rc.exe -ErrorAction SilentlyContinue)) { throw 'Windows SDK resource compiler missing from MSVC environment.' }
$grep = Get-Command grep.exe -ErrorAction SilentlyContinue
if (-not $grep -and -not (Test-Path -LiteralPath 'C:/Program Files/Git/usr/bin/grep.exe')) {
    throw "Trace conversion needs Git for Windows grep.exe. Add its directory to PATH."
}
Assert-Pins
$snapshot = Get-Content -LiteralPath "$WorkspaceRoot/snapshot.json" -Raw | ConvertFrom-Json
Assert-Hash (Join-Path $WorkspaceRoot $snapshot.magnusArchive.path) $snapshot.magnusArchive.sha256
Assert-Hash (Join-Path $WorkspaceRoot $snapshot.sourceBundle.path) $snapshot.sourceBundle.sha256
Write-Output 'Local prerequisites, pinned repositories, and source archives verified.'
Write-Output 'AMD Conan access is checked by Build-Magnus.ps1; no credentials were requested or changed.'
if ($WorkspaceRoot.Length -gt 30) { Write-Warning 'Use a short checkout path such as C:/RDNA to avoid native build path limits.' }