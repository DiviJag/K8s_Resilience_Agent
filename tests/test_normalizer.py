from chaosloop.live.normalizer import (
    group_alerts,
    service_of,
    triggers_from_alerts,
    triggers_from_webhook,
)


def alert(name: str, service: str | None = None, pod: str | None = None, started: str = "",
          state: str = "active", **labels: str) -> dict:
    lbl = {"alertname": name, "namespace": "chaosloop-lab", **labels}
    if service:
        lbl["service"] = service
    if pod:
        lbl["pod"] = pod
    return {
        "labels": lbl,
        "annotations": {"summary": f"{name} summary"},
        "startsAt": started,
        "status": {"state": state},
    }


def test_service_comes_from_labels_or_the_pod_name():
    assert service_of({"service": "cart-service"}) == "cart-service"
    assert service_of({"deployment": "billing-service"}) == "billing-service"
    assert service_of({"pod": "cart-service-6d4f8797f6-2lcfw"}) == "cart-service"
    assert service_of({}) == "unknown"


def test_one_incident_is_one_trigger():
    """Two alerts about the same workload are one investigation, not two (HLD §3)."""
    triggers = triggers_from_alerts([
        alert("PodNotReady", service="cart-service", started="2026-10-02T10:00:05Z"),
        alert("PodImagePullBackOff", pod="cart-service-6d4f8797f6-2lcfw",
              started="2026-10-02T10:00:01Z", severity="warning"),
    ])
    assert len(triggers) == 1
    trigger = triggers[0]
    assert trigger.alertname == "PodImagePullBackOff"  # earliest names the incident
    assert trigger.service == "cart-service"
    assert trigger.pod == "cart-service-6d4f8797f6-2lcfw"
    assert trigger.extra["also_firing"] == "PodNotReady"
    assert trigger.extra["severity"] == "warning"


def test_separate_workloads_are_separate_incidents():
    triggers = triggers_from_alerts([
        alert("PodCrashLooping", service="billing-service"),
        alert("UpstreamErrors", service="checkout-frontend"),
    ])
    assert [t.service for t in triggers] == ["billing-service", "checkout-frontend"]


def test_suppressed_and_resolved_alerts_are_ignored():
    assert group_alerts([alert("X", service="a", state="suppressed")]) == {}
    webhook = {"alerts": [{**alert("X", service="a"), "status": "resolved"}]}
    assert triggers_from_webhook(webhook) == []


def test_webhook_and_api_shapes_normalise_identically():
    api_alert = alert("PodCrashLooping", service="billing-service", started="t1")
    webhook_alert = {**api_alert, "status": "firing"}
    assert triggers_from_webhook({"alerts": [webhook_alert]}) == triggers_from_alerts([api_alert])


def test_trigger_matches_the_recorded_golden_shape():
    trigger = triggers_from_alerts([alert("PodPendingTooLong", service="gpu-scorer")])[0]
    rendered = trigger.render()
    assert "alertname: PodPendingTooLong" in rendered and "service: gpu-scorer" in rendered
