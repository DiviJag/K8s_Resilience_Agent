#requires -Version 5.1
<#
Brings up the Tier B lab (HLD §3, §9): kind cluster, two local registries (one open, one
password-protected), sample services, Prometheus + Alertmanager + kube-state-metrics, Chaos
Mesh, and least-privilege identities for the agent and the lifecycle controller.
Idempotent - safe to re-run; re-running also refreshes the short-lived service account tokens.
Every kubectl call pins --context because this machine has more than one kubectl on PATH and a
Docker Desktop cluster that must not be touched.
#>
[CmdletBinding()]
param(
    [string]$ClusterName = "chaosloop",
    [string]$RegistryName = "chaosloop-registry",
    [int]$RegistryPort = 5001,
    [string]$AuthRegistryName = "chaosloop-registry-auth",
    [int]$AuthRegistryPort = 5002,
    [string]$PrometheusChartVersion = "29.35.0",
    [string]$ChaosMeshChartVersion = "2.8.4",
    [string]$TokenDuration = "24h",
    [switch]$SkipMonitoring,
    [switch]$SkipChaosMesh
)

# 'Continue', with every native call judged by its exit code: kind, docker, helm and kubectl
# write progress and warnings to stderr, and under 'Stop' Windows PowerShell turns that stderr
# into a terminating error whenever the caller redirects it (2>&1, a pipe, a CI log capture).
$ErrorActionPreference = "Continue"
$utf8 = New-Object System.Text.UTF8Encoding $false
$repoRoot = Split-Path -Parent $PSScriptRoot
$context = "kind-$ClusterName"
$labNs = "chaosloop-lab"
$authDir = Join-Path $repoRoot "lab\.registry-auth"
$authInCluster = "${AuthRegistryName}:5000"

function Assert-Exit {
    param([string]$What)
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE" }
}

function Invoke-Kubectl {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$KubectlArgs)
    & kubectl --context $context @KubectlArgs
    Assert-Exit "kubectl $($KubectlArgs -join ' ')"
}

function Start-Registry {
    param([string]$Name, [int]$Port, [string[]]$ExtraArgs = @())
    $running = & docker ps --filter "name=^/$Name$" --format '{{.Names}}'
    if ($running) { return }
    $existing = & docker ps -a --filter "name=^/$Name$" --format '{{.Names}}'
    if ($existing) {
        & docker start $Name | Out-Null
        Assert-Exit "docker start $Name"
        return
    }
    & docker run -d --restart=always -p "127.0.0.1:${Port}:5000" --name $Name @ExtraArgs registry:2 |
        Out-Null
    if ($LASTEXITCODE -ne 0) {
        # Docker Desktop can drop the connection (EOF) the first time it shares a new host
        # folder, after the container is already created; starting it then succeeds.
        & docker start $Name | Out-Null
    }
    Assert-Exit "docker run $Name"
}

function Connect-KindNetwork {
    param([string]$Name)
    $connected = & docker inspect -f '{{json .NetworkSettings.Networks.kind}}' $Name
    if ($connected -eq "null") {
        & docker network connect kind $Name
        Assert-Exit "docker network connect $Name"
    }
}

function Write-NodeFile {
    # Written via a BOM-free temp file and `docker cp` rather than piped into `docker exec`:
    # a BOM makes containerd's TOML parse fail silently - it then ignores the mirror and falls
    # back to pulling from localhost inside the node, which always refuses the connection.
    param([string]$Node, [string]$Dir, [string]$Content)
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "chaosloop-hosts.toml"
    [System.IO.File]::WriteAllText($tmp, $Content, $utf8)
    try {
        & docker exec $Node mkdir -p $Dir
        Assert-Exit "mkdir $Dir on $Node"
        & docker cp $tmp "${Node}:$Dir/hosts.toml"
        Assert-Exit "install $Dir/hosts.toml on $Node"
    }
    finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
    }
}

function New-Password {
    $chars = [char[]]"abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

function Write-ServiceAccountKubeconfig {
    # A kubeconfig holding a short-lived token for one lab service account, so the agent and the
    # controller never run with the developer's cluster-admin credentials (HLD §11).
    param([string]$Account, [string]$Path)
    $server = & kubectl config view --raw --minify --context $context -o "jsonpath={.clusters[0].cluster.server}"
    Assert-Exit "read cluster server"
    $ca = & kubectl config view --raw --minify --context $context -o "jsonpath={.clusters[0].cluster.certificate-authority-data}"
    Assert-Exit "read cluster CA"
    $token = & kubectl --context $context -n $labNs create token $Account --duration=$TokenDuration
    Assert-Exit "create token for $Account"
    $yaml = @"
apiVersion: v1
kind: Config
clusters:
  - name: chaosloop
    cluster:
      server: $server
      certificate-authority-data: $ca
users:
  - name: $Account
    user:
      token: $token
contexts:
  - name: chaosloop-$Account
    context:
      cluster: chaosloop
      user: $Account
      namespace: $labNs
current-context: chaosloop-$Account
"@
    [System.IO.File]::WriteAllText($Path, $yaml.Replace("`r`n", "`n"), $utf8)
}

Write-Host "==> checking Docker" -ForegroundColor Cyan
$memBytes = & docker info --format '{{.MemTotal}}'
if ($LASTEXITCODE -ne 0) { throw "Docker is not running. Start Docker Desktop and retry." }
$memGb = [math]::Round([double]$memBytes / 1GB, 1)
if ($memGb -lt 5.5) {
    Write-Warning ("Docker has ${memGb} GB of memory; Prometheus and Chaos Mesh on three nodes need " +
        "about 6 GB. Raise it in Docker Desktop > Settings > Resources, or pass " +
        "-SkipMonitoring -SkipChaosMesh for a config-faults-only lab.")
}
else {
    Write-Host "    ${memGb} GB available to Docker"
}

Write-Host "==> local registries" -ForegroundColor Cyan
Start-Registry -Name $RegistryName -Port $RegistryPort

# Test credentials for the password-protected registry: generated once on this machine and kept
# in lab/.registry-auth (gitignored). They guard nothing but a busybox image.
New-Item -ItemType Directory -Force $authDir | Out-Null
$passwordFile = Join-Path $authDir "password"
$htpasswdFile = Join-Path $authDir "htpasswd"
if (-not (Test-Path $passwordFile) -or -not (Test-Path $htpasswdFile)) {
    $password = New-Password
    $output = & docker run --rm --entrypoint htpasswd httpd:2-alpine -Bbn chaosloop $password
    Assert-Exit "generate htpasswd"
    # -n prints the entry and then a blank line; interpolating both would join them with a
    # space and corrupt the hash.
    $line = @($output | Where-Object { "$_".Trim() })[0].Trim()
    [System.IO.File]::WriteAllText($htpasswdFile, "$line`n", $utf8)
    [System.IO.File]::WriteAllText($passwordFile, $password, $utf8)
    & docker rm -f $AuthRegistryName 2>$null | Out-Null  # it must reload the new htpasswd
}
$password = ([System.IO.File]::ReadAllText($passwordFile)).Trim()
Start-Registry -Name $AuthRegistryName -Port $AuthRegistryPort -ExtraArgs @(
    "--mount", "type=bind,source=$authDir,target=/auth,readonly",
    "-e", "REGISTRY_AUTH=htpasswd",
    "-e", "REGISTRY_AUTH_HTPASSWD_REALM=chaosloop",
    "-e", "REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd"
)

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

Write-Host "==> wiring nodes to the registries" -ForegroundColor Cyan
Connect-KindNetwork $RegistryName
Connect-KindNetwork $AuthRegistryName
$openToml = "[host.`"http://${RegistryName}:5000`"]`n  capabilities = [`"pull`", `"resolve`"]`n"
# The protected registry is pulled by its in-network name, so the pull secret's server matches
# the host containerd talks to; it only needs telling to use plain HTTP.
$authToml = "server = `"http://$authInCluster`"`n"
foreach ($node in (& kind get nodes --name $ClusterName)) {
    Write-NodeFile -Node $node -Dir "/etc/containerd/certs.d/localhost:$RegistryPort" -Content $openToml
    Write-NodeFile -Node $node -Dir "/etc/containerd/certs.d/$authInCluster" -Content $authToml
}

Write-Host "==> seeding images" -ForegroundColor Cyan
& docker pull busybox:1.36 | Out-Null
Assert-Exit "docker pull busybox:1.36"
foreach ($ref in @("busybox:1.36", "cart:v1.0.0")) {
    & docker tag busybox:1.36 "localhost:$RegistryPort/$ref"
    Assert-Exit "docker tag $ref"
    & docker push "localhost:$RegistryPort/$ref" | Out-Null
    Assert-Exit "docker push localhost:$RegistryPort/$ref"
}
# A throwaway Docker config dir, so logging in to the lab registry never touches the user's own
# ~/.docker credentials.
$env:DOCKER_CONFIG = Join-Path $authDir "docker-config"
try {
    # The password file goes straight to stdin: piping a string from Windows PowerShell mangles
    # it, and -p would put it on the command line.
    $login = Start-Process docker -NoNewWindow -Wait -PassThru `
        -ArgumentList @("login", "localhost:$AuthRegistryPort", "-u", "chaosloop", "--password-stdin") `
        -RedirectStandardInput $passwordFile `
        -RedirectStandardOutput (Join-Path $authDir "login.log")
    if ($login.ExitCode -ne 0) { throw "docker login localhost:$AuthRegistryPort failed with exit code $($login.ExitCode)" }
    & docker tag busybox:1.36 "localhost:$AuthRegistryPort/cart:v1.0.0"
    Assert-Exit "docker tag protected cart"
    & docker push "localhost:$AuthRegistryPort/cart:v1.0.0" | Out-Null
    Assert-Exit "docker push localhost:$AuthRegistryPort/cart:v1.0.0"
}
finally {
    Remove-Item Env:\DOCKER_CONFIG -ErrorAction SilentlyContinue
}

& kubectl config use-context $context | Out-Null
Assert-Exit "kubectl config use-context $context"

if (-not $SkipMonitoring) {
    Write-Host "==> Prometheus + Alertmanager (chart $PrometheusChartVersion)" -ForegroundColor Cyan
    & helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update | Out-Null
    Assert-Exit "helm repo add prometheus-community"
    & helm upgrade --install prometheus prometheus-community/prometheus `
        --kube-context $context -n monitoring --create-namespace `
        --version $PrometheusChartVersion `
        -f (Join-Path $repoRoot "lab\monitoring\prometheus-values.yaml") `
        --wait --timeout 10m
    Assert-Exit "helm install prometheus"
}

if (-not $SkipChaosMesh) {
    Write-Host "==> Chaos Mesh (chart $ChaosMeshChartVersion)" -ForegroundColor Cyan
    & helm repo add chaos-mesh https://charts.chaos-mesh.org --force-update | Out-Null
    Assert-Exit "helm repo add chaos-mesh"
    & helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh `
        --kube-context $context -n chaos-mesh --create-namespace `
        --version $ChaosMeshChartVersion `
        -f (Join-Path $repoRoot "lab\chaos\chaos-mesh-values.yaml") `
        --wait --timeout 10m
    Assert-Exit "helm install chaos-mesh"
}

Write-Host "==> lab workloads and identities" -ForegroundColor Cyan
Invoke-Kubectl apply -f (Join-Path $repoRoot "lab\services")
Invoke-Kubectl apply -f (Join-Path $repoRoot "lab\rbac")
# Recreated rather than applied so the password never sits in a manifest on disk.
& kubectl --context $context -n $labNs delete secret regcred --ignore-not-found | Out-Null
Invoke-Kubectl -n $labNs create secret docker-registry regcred `
    "--docker-server=$authInCluster" "--docker-username=chaosloop" "--docker-password=$password"
foreach ($deployment in @("billing-service", "cart-service", "analytics-worker",
        "reporting-service", "checkout-frontend")) {
    Invoke-Kubectl -n $labNs rollout status "deployment/$deployment" --timeout=180s
}

Write-ServiceAccountKubeconfig -Account investigator -Path (Join-Path $repoRoot "lab\.kubeconfig-investigator")
Write-ServiceAccountKubeconfig -Account injector -Path (Join-Path $repoRoot "lab\.kubeconfig-injector")
Write-Host "    wrote lab/.kubeconfig-investigator and lab/.kubeconfig-injector (valid $TokenDuration)"

Write-Host ""
Write-Host "lab is up. context=$context namespace=$labNs" -ForegroundColor Green
Write-Host "Prometheus http://127.0.0.1:9090   Alertmanager http://127.0.0.1:9093"
Write-Host "next: uv run chaosloop-lab status    then    uv run chaosloop-lab run --plan 3"
