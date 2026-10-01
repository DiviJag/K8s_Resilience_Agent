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

$ErrorActionPreference = "Stop"

$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    # Native stderr ("No kind clusters found.") would otherwise terminate under -Stop.
    $clusters = @(& kind get clusters 2>&1 | ForEach-Object { "$_".Trim() } |
        Where-Object { $_ -and $_ -notmatch "No kind clusters found" })
}
finally {
    $ErrorActionPreference = $previous
}

if ($clusters -contains $ClusterName) {
    Write-Host "==> deleting kind cluster $ClusterName" -ForegroundColor Cyan
    & kind delete cluster --name $ClusterName
}
else {
    Write-Host "cluster $ClusterName is not running"
}

if ($RemoveRegistry) {
    Write-Host "==> removing registry $RegistryName" -ForegroundColor Cyan
    & docker rm -f $RegistryName | Out-Null
}
else {
    Write-Host "registry $RegistryName left running (pass -RemoveRegistry to delete it)"
}
