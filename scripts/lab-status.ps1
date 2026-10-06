#requires -Version 5.1
[CmdletBinding()]
param([string]$ClusterName = "chaosloop", [string]$RegistryName = "chaosloop-registry")

$ErrorActionPreference = "Continue"
$context = "kind-$ClusterName"

Write-Host "==> registry" -ForegroundColor Cyan
& docker ps --filter "name=^/$RegistryName$" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

Write-Host ""
Write-Host "==> nodes" -ForegroundColor Cyan
& kubectl --context $context get nodes

Write-Host ""
Write-Host "==> lab workloads" -ForegroundColor Cyan
& kubectl --context $context -n chaosloop-lab get deploy,pods,svc,pvc

Write-Host ""
Write-Host "==> recent warnings" -ForegroundColor Cyan
& kubectl --context $context -n chaosloop-lab get events --field-selector type=Warning --sort-by=.lastTimestamp

Write-Host ""
Write-Host "==> monitoring and chaos" -ForegroundColor Cyan
& kubectl --context $context get pods -n monitoring
& kubectl --context $context get pods -n chaos-mesh

Write-Host ""
Write-Host "==> steady state" -ForegroundColor Cyan
& uv run chaosloop-lab status
