#Requires -Version 5.1
<#
.SYNOPSIS
    Create the docparse virtual environment and verify the toolkit works.
#>
[CmdletBinding()]
param(
    [string]$DocsRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'Docs'),
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host 'Creating .venv ...' -ForegroundColor Cyan
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Creating the Agent environment failed.' }
}

Write-Host 'Installing dependencies ...' -ForegroundColor Cyan
& $venvPython -m pip install --disable-pip-version-check --quiet --requirement requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Installing Agent dependencies failed.' }

Write-Host 'Registered parsers:' -ForegroundColor Cyan
& $venvPython -m docparse parsers
if ($LASTEXITCODE -ne 0) { throw 'Parser registration check failed.' }

if (-not $SkipTests) {
    if (Test-Path $DocsRoot) {
        Write-Host "`nParsing a sample of every file type under $DocsRoot ..." -ForegroundColor Cyan
        & $venvPython tests\smoke_corpus.py $DocsRoot --per-ext 2 --max-bytes 30000000
        if ($LASTEXITCODE -ne 0) { throw 'Document corpus smoke test failed.' }
    }
    else {
        Write-Warning "Docs root not found: $DocsRoot (skipping the corpus test)"
    }
    Write-Host "`nChecking the MCP server ..." -ForegroundColor Cyan
    & $venvPython tests\smoke_mcp.py
    if ($LASTEXITCODE -ne 0) { throw 'MCP smoke test failed.' }
}

Write-Host "`nReady. Try:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\python.exe -m docparse search `"$DocsRoot\gfx12`" -p `"wave64`" -i --jobs 8"
