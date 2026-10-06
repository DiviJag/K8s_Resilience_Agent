#requires -Version 5.1
<#
Tears down the lab cluster. The registry containers are left running by default so repeated
lab-up cycles do not re-pull images; pass -RemoveRegistry to delete them too. Leftover
experiment journals (lab/.journal) are cleared with the cluster they described.
#>
[CmdletBinding()]
param(
    [string]$ClusterName = "chaosloop",
    [string]$RegistryName = "chaosloop-registry",
    [string]$AuthRegistryName = "chaosloop-registry-auth",
    [switch]$RemoveRegistry
)

# 'Continue' with exit codes checked explicitly - see scripts/lab-up.ps1 for why 'Stop' breaks
# whenever the caller redirects a native tool's stderr.
$ErrorActionPreference = "Continue"

# "No kind clusters found." arrives on stderr; keep only real cluster names.
$clusters = @(& kind get clusters 2>&1 | ForEach-Object { "$_".Trim() } |
    Where-Object { $_ -and $_ -notmatch "No kind clusters found" })

if ($clusters -contains $ClusterName) {
    Write-Host "==> deleting kind cluster $ClusterName" -ForegroundColor Cyan
    & kind delete cluster --name $ClusterName
    if ($LASTEXITCODE -ne 0) { throw "kind delete cluster failed with exit code $LASTEXITCODE" }
}
else {
    Write-Host "cluster $ClusterName is not running"
}

$journal = Join-Path (Split-Path -Parent $PSScriptRoot) "lab\.journal"
Remove-Item -Recurse -Force $journal -ErrorAction SilentlyContinue

if ($RemoveRegistry) {
    foreach ($name in @($RegistryName, $AuthRegistryName)) {
        Write-Host "==> removing registry $name" -ForegroundColor Cyan
        & docker rm -f $name | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "failed to remove $name" }
    }
}
else {
    Write-Host "registries left running (pass -RemoveRegistry to delete them)"
}
