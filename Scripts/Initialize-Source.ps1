[CmdletBinding()]
param()
. "$PSScriptRoot/Common.ps1"
Assert-Pins
$snapshot = Get-Content -LiteralPath (Join-Path $WorkspaceRoot 'snapshot.json') -Raw | ConvertFrom-Json
$pending = @()
foreach ($entry in $snapshot.overlay) {
    $source = Get-SafePath $WorkspaceRoot $entry.source
    $target = Get-SafePath $WorkspaceRoot $entry.target
    Assert-Hash $source $entry.sha256
    if (Test-Path -LiteralPath $target) {
        if ((Get-FileHash -LiteralPath $target).Hash -eq $entry.sha256) { continue }
        $relative = $entry.target.Substring('GpuKernelLab/'.Length)
        $blob = git --no-optional-locks -C (Join-Path $WorkspaceRoot 'GpuKernelLab') hash-object "--path=$relative" -- $target
        if ($LASTEXITCODE -ne 0 -or $blob -ne $entry.baseBlob) { throw "Conflicting local edit: $($entry.target). No overlay files changed." }
    }
    $pending += [pscustomobject]@{Source=$source;Target=$target;Hash=$entry.sha256}
}
$archive = Join-Path $WorkspaceRoot $snapshot.magnusArchive.path
Assert-Hash $archive $snapshot.magnusArchive.sha256
if (-not (Test-Path -LiteralPath $ModelSource)) {
    $members = @(tar -tzf $archive)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot list source archive' }
    foreach ($member in $members) { Get-SafePath $ModelSource $member | Out-Null }
    [IO.Directory]::CreateDirectory($ModelSource) | Out-Null
    Invoke-Checked tar @('-xzf',$archive,'-C',$ModelSource)
    [IO.File]::WriteAllText((Join-Path $ModelSource '.migration-archive.sha256'), $snapshot.magnusArchive.sha256)
} else {
    $marker = Join-Path $ModelSource '.migration-archive.sha256'
    if (-not (Test-Path -LiteralPath $marker) -or (Get-Content -LiteralPath $marker -Raw).Trim() -ne $snapshot.magnusArchive.sha256) {
        throw 'Existing Magnus directory was not extracted by this setup. Use a fresh destination; nothing will be overwritten.'
    }
}
foreach ($entry in $pending) {
    [IO.Directory]::CreateDirectory((Split-Path -Parent $entry.Target)) | Out-Null
    Copy-Item -LiteralPath $entry.Source -Destination $entry.Target
    Assert-Hash $entry.Target $entry.Hash
}
Write-Output "Source initialized; $($pending.Count) saved GpuKernelLab edits restored. No Git index or branch was changed."