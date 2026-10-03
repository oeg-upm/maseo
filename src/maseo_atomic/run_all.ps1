# Run the MASEO atomic pipeline for several domains, one after another
# (Windows PowerShell; run_all.sh does the same on Linux and macOS).
#
#   .\run_all.ps1                      every domain with a dataset\<domain>_cq2onto_cqs.json
#   .\run_all.ps1 wine swo             only these domains
#   .\run_all.ps1 -Config my.yaml      with another config file (default: config.yaml)
#
# If Windows refuses to run scripts, allow them for this window only:
#   Set-ExecutionPolicy -Scope Process Bypass
#
# A failed domain does not stop the others; the summary at the end lists it.
param(
    [string]$Config = "config.yaml",
    [string]$Python = "python",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Domains
)

Set-Location -LiteralPath $PSScriptRoot
# UTF-8 for Python and the MCP servers it starts, whatever the Windows code page
$env:PYTHONUTF8 = "1"

if (-not $Domains -or $Domains.Count -eq 0) {
    $Domains = @(Get-ChildItem -Path "dataset" -Filter "*_cq2onto_cqs.json" |
                 ForEach-Object { $_.Name -replace "_cq2onto_cqs\.json$", "" } |
                 Sort-Object)
}
if ($Domains.Count -eq 0) {
    Write-Host "No domains: dataset\ has no *_cq2onto_cqs.json"
    exit 1
}

$failed = @()
foreach ($d in $Domains) {
    Write-Host "=== $d"
    & $Python -u agent_graph.py $d --config $Config
    if ($LASTEXITCODE -ne 0) { $failed += $d }
}

Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host ("Finished with errors in: " + ($failed -join " "))
    exit 1
}
Write-Host ("Finished: " + $Domains.Count + " domain(s)")
