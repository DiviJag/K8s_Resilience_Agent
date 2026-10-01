#requires -Version 5.1
<#
Returns the lab to steady state after an injection. The manifests under lab/services are
the snapshot, so re-applying them reverts any patched field; the extras below are objects
an injection created or state that lives outside those manifests.
#>
[CmdletBinding()]
param([string]$ClusterName = "chaosloop", [string]$Namespace = "chaosloop-lab")

$ErrorActionPreference = "Stop"
$context = "kind-$ClusterName"
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

Write-Host "==> restoring manifests" -ForegroundColor Cyan
# Deployments are deleted rather than re-applied: `kubectl apply` only removes fields that were
# in last-applied-configuration, so a field an injection *added* (nodeSelector, livenessProbe)
# survives an apply and the lab never returns to steady state. Services and ConfigMaps are
# patched in place by injections, so apply restores those correctly.
& kubectl --context $context -n $Namespace delete deployment --all --ignore-not-found --wait
if ($LASTEXITCODE -ne 0) { throw "restore failed to delete lab deployments" }

& kubectl --context $context apply -f (Join-Path $repoRoot "lab\services")
if ($LASTEXITCODE -ne 0) { throw "restore failed to apply lab/services" }

Write-Host "==> removing injected objects" -ForegroundColor Cyan
& kubectl --context $context -n $Namespace delete resourcequota chaosloop-experiment-quota --ignore-not-found
& kubectl --context $context -n $Namespace delete pvc reporting-data-broken --ignore-not-found

Write-Host "==> clearing taints" -ForegroundColor Cyan
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    # Removing an absent taint is an expected no-op, and its stderr must not terminate us.
    foreach ($node in (& kind get nodes --name $ClusterName)) {
        & kubectl --context $context taint nodes $node chaosloop- 2>&1 | Out-Null
    }
}
finally {
    $ErrorActionPreference = $previous
}

Write-Host "==> waiting for steady state" -ForegroundColor Cyan
foreach ($deployment in @("billing-service", "cart-service", "analytics-worker", "reporting-service")) {
    & kubectl --context $context -n $Namespace rollout status "deployment/$deployment" --timeout=120s
}

Write-Host ""
Write-Host "lab restored to steady state" -ForegroundColor Green
