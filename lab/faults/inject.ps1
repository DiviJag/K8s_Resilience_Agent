#requires -Version 5.1
<#
Reproducible, hand-run fault injection for Track B recordings (HLD §14 Phase 1:
"break the lab by hand"). Each category is a named, repeatable mutation - deliberately not
a chaos framework; automated Chaos Mesh experiments are Phase 3.

Because we cause the fault, we know the true category: that is what makes the recording a
labelled test case.

    lab/faults/inject.ps1 -Category registry_auth_missing
    lab/faults/restore.ps1
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "missing_env_var",
        "bad_config_value",
        "failing_liveness_probe",
        "image_tag_not_found",
        "registry_auth_missing",
        "registry_unreachable",
        "memory_limit_too_low",
        "insufficient_resources",
        "node_selector_mismatch",
        "taint_not_tolerated",
        "pvc_unbound",
        "quota_exceeded",
        "progress_deadline_exceeded",
        "selector_mismatch",
        "target_port_mismatch",
        "readiness_probe_misconfigured"
    )]
    [string]$Category,
    [string]$ClusterName = "chaosloop",
    [string]$Namespace = "chaosloop-lab"
)

$ErrorActionPreference = "Stop"
$context = "kind-$ClusterName"

function Invoke-K {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & kubectl --context $context -n $Namespace @Args
    if ($LASTEXITCODE -ne 0) { throw "kubectl $($Args -join ' ') failed ($LASTEXITCODE)" }
}

function Invoke-JsonPatch {
    param([string]$Target, [string]$Patch)
    $file = [System.IO.Path]::GetTempFileName()
    try {
        Set-Content -Path $file -Value $Patch -Encoding ascii
        Invoke-K patch $Target --type=json --patch-file $file
    }
    finally {
        Remove-Item $file -ErrorAction SilentlyContinue
    }
}

function Set-Image {
    param([string]$Deployment, [string]$Container, [string]$Image)
    Invoke-JsonPatch -Target "deployment/$Deployment" -Patch `
    "[{""op"":""replace"",""path"":""/spec/template/spec/containers/0/image"",""value"":""$Image""}]"
}

Write-Host "==> injecting $Category into $Namespace" -ForegroundColor Yellow

switch ($Category) {
    "missing_env_var" {
        # DATABASE_URL is the only thing keeping billing's entrypoint alive.
        Invoke-JsonPatch -Target "deployment/billing-service" -Patch `
            '[{"op":"replace","path":"/spec/template/spec/containers/0/env","value":[]}]'
    }
    "bad_config_value" {
        Invoke-JsonPatch -Target "configmap/billing-config" -Patch `
            '[{"op":"replace","path":"/data/MAX_WORKERS","value":"many"}]'
        Invoke-K rollout restart deployment/billing-service
    }
    "failing_liveness_probe" {
        Invoke-JsonPatch -Target "deployment/billing-service" -Patch `
            ('[{"op":"add","path":"/spec/template/spec/containers/0/livenessProbe",' +
            '"value":{"tcpSocket":{"port":9999},"initialDelaySeconds":5,"periodSeconds":5,' +
            '"failureThreshold":1}}]')
    }
    "image_tag_not_found" {
        Set-Image -Deployment cart-service -Container cart -Image "localhost:5001/cart:v9.9.9"
    }
    "registry_auth_missing" {
        # A private GHCR path: the pull is anonymous, so the registry answers with a denial.
        Set-Image -Deployment cart-service -Container cart `
            -Image "ghcr.io/chaosloop-private/cart:v1.0.0"
    }
    "registry_unreachable" {
        Set-Image -Deployment cart-service -Container cart `
            -Image "registry.invalid.chaosloop.local/cart:v1.0.0"
    }
    "memory_limit_too_low" {
        # Working set (256MB into tmpfs) now far exceeds the 96Mi limit.
        Invoke-JsonPatch -Target "deployment/analytics-worker" -Patch `
            '[{"op":"replace","path":"/spec/template/spec/containers/0/env/0/value","value":"256"}]'
    }
    "insufficient_resources" {
        Invoke-JsonPatch -Target "deployment/analytics-worker" -Patch `
            ('[{"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/memory"' +
            ',"value":"64Gi"}]')
    }
    "node_selector_mismatch" {
        Invoke-JsonPatch -Target "deployment/analytics-worker" -Patch `
            '[{"op":"add","path":"/spec/template/spec/nodeSelector","value":{"accelerator":"nvidia-t4"}}]'
    }
    "taint_not_tolerated" {
        foreach ($node in (& kind get nodes --name $ClusterName)) {
            if ($node -like "*worker*") {
                & kubectl --context $context taint nodes $node chaosloop=experiment:NoSchedule --overwrite
            }
        }
        Invoke-K rollout restart deployment/analytics-worker
    }
    "pvc_unbound" {
        $claim = @"
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: reporting-data-broken
  namespace: $Namespace
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: chaosloop-nonexistent
  resources:
    requests:
      storage: 128Mi
"@
        $file = [System.IO.Path]::GetTempFileName()
        try {
            Set-Content -Path $file -Value $claim -Encoding ascii
            Invoke-K apply -f $file
        }
        finally { Remove-Item $file -ErrorAction SilentlyContinue }
        Invoke-JsonPatch -Target "deployment/reporting-service" -Patch `
            ('[{"op":"replace","path":"/spec/template/spec/volumes/0/persistentVolumeClaim/claimName"' +
            ',"value":"reporting-data-broken"}]')
    }
    "quota_exceeded" {
        $quota = @"
apiVersion: v1
kind: ResourceQuota
metadata:
  name: chaosloop-experiment-quota
  namespace: $Namespace
spec:
  hard:
    pods: "4"
"@
        $file = [System.IO.Path]::GetTempFileName()
        try {
            Set-Content -Path $file -Value $quota -Encoding ascii
            Invoke-K apply -f $file
        }
        finally { Remove-Item $file -ErrorAction SilentlyContinue }
        Invoke-K scale deployment/cart-service --replicas=6
    }
    "progress_deadline_exceeded" {
        Invoke-JsonPatch -Target "deployment/cart-service" -Patch `
            '[{"op":"replace","path":"/spec/progressDeadlineSeconds","value":30}]'
        Set-Image -Deployment cart-service -Container cart -Image "localhost:5001/cart:v9.9.9"
    }
    "selector_mismatch" {
        Invoke-JsonPatch -Target "service/cart-service" -Patch `
            '[{"op":"replace","path":"/spec/selector","value":{"app":"cart-service-v2"}}]'
    }
    "target_port_mismatch" {
        Invoke-JsonPatch -Target "service/reporting-service" -Patch `
            '[{"op":"replace","path":"/spec/ports/0/targetPort","value":9999}]'
    }
    "readiness_probe_misconfigured" {
        Invoke-JsonPatch -Target "deployment/reporting-service" -Patch `
            '[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/tcpSocket/port","value":9999}]'
    }
}

Write-Host ""
Write-Host "injected: $Category" -ForegroundColor Yellow
Write-Host "watch it land:  kubectl --context $context -n $Namespace get pods -w"
Write-Host "record it:      uv run chaosloop-record capture --category $Category ..."
Write-Host "undo it:        lab/faults/restore.ps1"
