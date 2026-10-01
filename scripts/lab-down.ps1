#requires -Version 5.1
<#
Tears down the lab cluster. The registry container is left running by default so repeated
lab-up cycles do not re-pull images; pass -RemoveRegistry to delete it too.
#>
[CmdletBinding()]
param(
    [string]$ClusterName = "chaosloop",
    [string]$RegistryName = "chaosloop-registry",
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

if ($RemoveRegistry) {
    Write-Host "==> removing registry $RegistryName" -ForegroundColor Cyan
    & docker rm -f $RegistryName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "failed to remove $RegistryName" }
}
else {
    Write-Host "registry $RegistryName left running (pass -RemoveRegistry to delete it)"
}
