#requires -Version 5.1
<#
Brings up the Tier B lab: kind cluster + local registry + sample services (HLD §3).
Idempotent - safe to re-run. Every kubectl call pins --context because this machine has
more than one kubectl on PATH and a Docker Desktop cluster that must not be touched.
#>
[CmdletBinding()]
param(
    [string]$ClusterName = "chaosloop",
    [string]$RegistryName = "chaosloop-registry",
    [int]$RegistryPort = 5001
)

# 'Continue', with every native call judged by its exit code: kind, docker and kubectl write
# progress and warnings to stderr, and under 'Stop' Windows PowerShell turns that stderr into a
# terminating error whenever the caller redirects it (2>&1, a pipe, a CI log capture).
$ErrorActionPreference = "Continue"
$repoRoot = Split-Path -Parent $PSScriptRoot
$context = "kind-$ClusterName"
$registryDir = "/etc/containerd/certs.d/localhost:$RegistryPort"

function Assert-Exit {
    param([string]$What)
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE" }
}

function Invoke-Kubectl {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$KubectlArgs)
    & kubectl --context $context @KubectlArgs
    Assert-Exit "kubectl $($KubectlArgs -join ' ')"
}

Write-Host "==> checking Docker" -ForegroundColor Cyan
& docker info --format '{{.ServerVersion}}' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker is not running. Start Docker Desktop and retry." }

Write-Host "==> local registry ($RegistryName on :$RegistryPort)" -ForegroundColor Cyan
$running = & docker ps --filter "name=^/$RegistryName$" --format '{{.Names}}'
if (-not $running) {
    $existing = & docker ps -a --filter "name=^/$RegistryName$" --format '{{.Names}}'
    if ($existing) {
        & docker start $RegistryName | Out-Null
        Assert-Exit "docker start $RegistryName"
    }
    else {
        & docker run -d --restart=always -p "127.0.0.1:${RegistryPort}:5000" `
            --name $RegistryName registry:2 | Out-Null
        Assert-Exit "docker run $RegistryName"
    }
}

Write-Host "==> kind cluster ($ClusterName)" -ForegroundColor Cyan
# "No kind clusters found." arrives on stderr; keep only real cluster names.
$clusters = @(& kind get clusters 2>&1 | ForEach-Object { "$_".Trim() } |
    Where-Object { $_ -and $_ -notmatch "No kind clusters found" })
if ($clusters -notcontains $ClusterName) {
    & kind create cluster --name $ClusterName --config (Join-Path $repoRoot "lab\kind-config.yaml")
    Assert-Exit "kind create cluster"
}
else {
    Write-Host "    already exists"
}

Write-Host "==> wiring nodes to the registry" -ForegroundColor Cyan
# Written via a BOM-free temp file and `docker cp` rather than piped into `docker exec`:
# Windows PowerShell prepends a UTF-8 BOM when it pipes text to a native command, and a BOM
# makes containerd's TOML parse fail silently - it then ignores the mirror and falls back to
# pulling from localhost inside the node, which always refuses the connection.
$hostsToml = "[host.`"http://${RegistryName}:5000`"]`n  capabilities = [`"pull`", `"resolve`"]`n"
$tomlPath = Join-Path ([System.IO.Path]::GetTempPath()) "chaosloop-hosts.toml"
[System.IO.File]::WriteAllText($tomlPath, $hostsToml, (New-Object System.Text.UTF8Encoding $false))
try {
    foreach ($node in (& kind get nodes --name $ClusterName)) {
        & docker exec $node mkdir -p $registryDir
        Assert-Exit "mkdir certs.d on $node"
        & docker cp $tomlPath "${node}:$registryDir/hosts.toml"
        Assert-Exit "install hosts.toml on $node"
    }
}
finally {
    Remove-Item $tomlPath -ErrorAction SilentlyContinue
}

$connected = & docker inspect -f '{{json .NetworkSettings.Networks.kind}}' $RegistryName
if ($connected -eq "null") {
    & docker network connect kind $RegistryName
    Assert-Exit "docker network connect"
}

Write-Host "==> seeding images into the registry" -ForegroundColor Cyan
& docker pull busybox:1.36 | Out-Null
Assert-Exit "docker pull busybox:1.36"
foreach ($ref in @("busybox:1.36", "cart:v1.0.0")) {
    & docker tag busybox:1.36 "localhost:$RegistryPort/$ref"
    Assert-Exit "docker tag $ref"
    & docker push "localhost:$RegistryPort/$ref" | Out-Null
    Assert-Exit "docker push localhost:$RegistryPort/$ref"
}

Write-Host "==> applying lab manifests" -ForegroundColor Cyan
& kubectl config use-context $context | Out-Null
Assert-Exit "kubectl config use-context $context"
Invoke-Kubectl apply -f (Join-Path $repoRoot "lab\services")
Invoke-Kubectl apply -f (Join-Path $repoRoot "lab\rbac\investigator.yaml")
foreach ($deployment in @("billing-service", "cart-service", "analytics-worker", "reporting-service")) {
    Invoke-Kubectl -n chaosloop-lab rollout status "deployment/$deployment" --timeout=180s
}

Write-Host ""
Write-Host "lab is up. context=$context namespace=chaosloop-lab" -ForegroundColor Green
Write-Host "next: ./scripts/lab-status.ps1   or   lab/faults/inject.ps1 -Category registry_auth_missing"
