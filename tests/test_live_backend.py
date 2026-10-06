"""LiveBackend projections, driven by stubbed API objects - no cluster required.

The point of these tests is the contract from HLD §2.2: what LiveBackend returns must have
the same shape as what a golden case records, or replay stops being a faithful substitute.
"""

from datetime import datetime
from types import SimpleNamespace as NS

import pytest

from chaosloop.tools.interface import NOT_AVAILABLE
from chaosloop.tools.live_backend import LiveBackend, _quantity

STARTED = datetime(2026, 9, 30, 11, 2, 41)


class FakeApiException(Exception):
    def __init__(self, status: int, reason: str = "Not Found") -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def waiting_state(reason: str, message: str) -> NS:
    return NS(waiting=NS(reason=reason, message=message), terminated=None, running=None)


def empty_state() -> NS:
    return NS(waiting=None, terminated=None, running=None)


def fake_pod() -> NS:
    return NS(
        metadata=NS(name="cart-service-abc", namespace="chaosloop-lab"),
        spec=NS(node_name="chaosloop-worker"),
        status=NS(
            phase="Pending",
            start_time=STARTED,
            conditions=[NS(type="Ready", status="False", reason="ContainersNotReady", message="x")],
            container_statuses=[
                NS(
                    name="cart",
                    ready=False,
                    restart_count=0,
                    image="localhost:5001/cart:v9.9.9",
                    state=waiting_state("ImagePullBackOff", "Back-off pulling image"),
                    last_state=empty_state(),
                )
            ],
        ),
    )


def fake_deployment() -> NS:
    container = NS(
        name="cart",
        image="localhost:5001/cart:v9.9.9",
        command=["/bin/sh"],
        args=["-c", "sleep"],
        ports=[NS(container_port=8080)],
        env=[
            NS(name="LOG_LEVEL", value="info", value_from=None),
            NS(
                name="DB_PASSWORD",
                value=None,
                value_from=NS(
                    secret_key_ref=NS(name="cart-db", key="password"), config_map_key_ref=None
                ),
            ),
        ],
        env_from=[NS(config_map_ref=NS(name="cart-config"))],
        resources=NS(requests={"cpu": "10m", "memory": "16Mi"}, limits={"memory": "64Mi"}),
        liveness_probe=None,
        readiness_probe=NS(
            initial_delay_seconds=3,
            period_seconds=5,
            failure_threshold=3,
            http_get=None,
            tcp_socket=NS(port=8080),
            exec=None,
        ),
        volume_mounts=[NS(name="data", mount_path="/data")],
    )
    return NS(
        metadata=NS(name="cart-service", namespace="chaosloop-lab", generation=4),
        spec=NS(
            replicas=2,
            progress_deadline_seconds=600,
            strategy=NS(type="RollingUpdate"),
            selector=NS(match_labels={"app": "cart-service"}),
            template=NS(
                metadata=NS(labels={"app": "cart-service"}),
                spec=NS(
                    node_selector=None,
                    tolerations=[],
                    image_pull_secrets=[NS(name="regcred")],
                    volumes=[
                        NS(
                            name="data",
                            persistent_volume_claim=NS(claim_name="cart-data"),
                            config_map=None,
                            empty_dir=None,
                        )
                    ],
                    containers=[container],
                ),
            ),
        ),
        status=NS(
            replicas=3,
            ready_replicas=2,
            updated_replicas=1,
            unavailable_replicas=1,
            conditions=[
                NS(type="Progressing", status="True", reason="ReplicaSetUpdated", message="m")
            ],
        ),
    )


class FakeCore:
    def __init__(self, log_text: str = "boot", previous_fails: bool = False) -> None:
        self.log_text = log_text
        self.previous_fails = previous_fails

    def read_namespaced_pod(self, name, namespace):
        if name == "missing":
            raise FakeApiException(404)
        return fake_pod()

    def read_namespaced_pod_log(self, name, namespace, container, previous, tail_lines):
        if previous and self.previous_fails:
            raise FakeApiException(400, "previous terminated container not found")
        return f"{'PREV ' if previous else ''}{self.log_text}"

    def list_namespaced_event(self, namespace, field_selector=None):
        return NS(
            items=[
                NS(
                    type="Warning",
                    reason="Failed",
                    count=3,
                    message='Failed to pull image: token=abcdefghijklmnopqrst',
                    last_timestamp=STARTED,
                    event_time=None,
                    involved_object=NS(name="cart-service-abc"),
                )
            ]
        )

    def read_namespaced_service(self, name, namespace):
        return NS(
            metadata=NS(name=name, namespace=namespace),
            spec=NS(
                type="ClusterIP",
                selector={"app": "cart-service"},
                ports=[NS(port=80, target_port=8080, protocol="TCP", name=None)],
            ),
        )

    def list_node(self):
        return NS(
            items=[
                NS(
                    metadata=NS(name="chaosloop-worker", labels={"kubernetes.io/os": "linux"}),
                    spec=NS(taints=[]),
                    status=NS(
                        conditions=[NS(type="Ready", status="True", reason="KubeletReady")],
                        allocatable={"cpu": "4", "memory": "8038084Ki"},
                    ),
                )
            ]
        )

    def list_pod_for_all_namespaces(self):
        return NS(
            items=[
                NS(
                    spec=NS(
                        node_name="chaosloop-worker",
                        containers=[NS(resources=NS(requests={"cpu": "100m", "memory": "128Mi"}))],
                    ),
                    status=NS(phase="Running"),
                )
            ]
        )


class FakeApps:
    def read_namespaced_deployment(self, name, namespace):
        return fake_deployment()


@pytest.fixture
def backend() -> LiveBackend:
    return LiveBackend(core_api=FakeCore(), apps_api=FakeApps())


def test_pod_status_shape_matches_recorded_cases(backend: LiveBackend):
    status = backend.get_pod_status("chaosloop-lab", "cart-service-abc")
    assert set(status) >= {"name", "namespace", "phase", "containerStatuses"}
    container = status["containerStatuses"][0]
    assert container["state"]["waiting"]["reason"] == "ImagePullBackOff"
    assert container["restartCount"] == 0


def test_missing_object_is_evidence_not_an_error(backend: LiveBackend):
    assert backend.get_pod_status("chaosloop-lab", "missing") == NOT_AVAILABLE


def test_logs_always_include_previous(backend: LiveBackend):
    logs = backend.get_logs("chaosloop-lab", "cart-service-abc")
    assert set(logs) == {"current", "previous"}
    assert logs["previous"].startswith("PREV")


def test_logs_tolerate_a_container_that_never_restarted():
    backend = LiveBackend(core_api=FakeCore(previous_fails=True), apps_api=FakeApps())
    logs = backend.get_logs("chaosloop-lab", "cart-service-abc")
    assert logs["current"] == "boot"
    assert "error" in str(logs["previous"])


def test_byte_logs_are_decoded_not_repr_d():
    class BytesCore(FakeCore):
        def read_namespaced_pod_log(self, name, namespace, container, previous, tail_lines):
            return b"[billing] boot\n[billing] FATAL config: DATABASE_URL is not set\n"

    logs = LiveBackend(core_api=BytesCore(), apps_api=FakeApps()).get_logs("ns", "p")
    assert logs["current"].splitlines()[1] == "[billing] FATAL config: DATABASE_URL is not set"
    assert not logs["current"].startswith("b'")


def test_logs_are_truncated():
    backend = LiveBackend(core_api=FakeCore(log_text="x" * 20_000), apps_api=FakeApps())
    assert "[truncated]" in backend.get_logs("chaosloop-lab", "p")["current"]


def test_events_are_redacted(backend: LiveBackend):
    events = backend.get_events("chaosloop-lab", "cart-service-abc")
    assert "abcdefghijklmnopqrst" not in events[0]["message"]
    assert events[0]["reason"] == "Failed"


def test_events_include_owned_objects_but_not_neighbours():
    """A quota failure exists only as a ReplicaSet event, so a deployment's events must
    include its ReplicaSets' - without pulling in a similarly named neighbour's."""

    def event(name: str, message: str) -> NS:
        return NS(
            type="Warning", reason="X", count=1, message=message, last_timestamp=STARTED,
            event_time=None, involved_object=NS(name=name),
        )

    class Core(FakeCore):
        def list_namespaced_event(self, namespace, field_selector=None):
            return NS(items=[
                event("cart-service-6d4f8797f6", "exceeded quota: pods=4"),
                event("cart-service-6d4f8797f6-2lcfw", "pod event"),
                event("cart-6b8d4f7c92-8kdnv", "a different workload"),
            ])

    events = LiveBackend(core_api=Core(), apps_api=FakeApps()).get_events("ns", "cart-service")
    assert [e["message"] for e in events] == ["exceeded quota: pods=4", "pod event"]


def test_chaos_mesh_events_never_reach_the_agent():
    """They name the injected fault outright - the answer key (HLD §11)."""

    def event(message: str, **extra) -> NS:
        return NS(
            type="Warning", reason="X", count=1, message=message, last_timestamp=STARTED,
            event_time=None,
            involved_object=extra.get("involved", NS(name="cart-service-6d4f8797f6-2lcfw",
                                                     api_version="v1")),
            source=NS(component=extra.get("component", "kubelet")),
            reporting_component=extra.get("reporter", ""),
        )

    class Core(FakeCore):
        def list_namespaced_event(self, namespace, field_selector=None):
            return NS(items=[
                event("Liveness probe failed"),
                event("NetworkChaos cart-partition applied",
                      involved=NS(name="cart-partition", api_version="chaos-mesh.org/v1alpha1")),
                event("injected network delay", component="chaos-daemon"),
                event("experiment started", reporter="chaos-controller-manager"),
            ])

    events = LiveBackend(core_api=Core(), apps_api=FakeApps()).get_events("ns")
    assert [e["message"] for e in events] == ["Liveness probe failed"]


def test_deployment_projection_keeps_diagnostic_fields(backend: LiveBackend):
    spec = backend.get_deployment_spec("chaosloop-lab", "cart-service")
    pod_spec = spec["spec"]["template"]["spec"]
    assert pod_spec["imagePullSecrets"] == [{"name": "regcred"}]
    assert pod_spec["containers"][0]["image"] == "localhost:5001/cart:v9.9.9"
    assert spec["status"]["unavailableReplicas"] == 1


def test_secret_reference_visible_but_never_resolved(backend: LiveBackend):
    env = backend.get_deployment_spec("chaosloop-lab", "cart-service")["spec"]["template"]["spec"][
        "containers"
    ][0]["env"]
    secret_ref = next(e for e in env if e["name"] == "DB_PASSWORD")
    assert secret_ref["valueFrom"]["secretKeyRef"] == {"name": "cart-db", "key": "password"}
    assert "value" not in secret_ref


def test_node_status_reports_requests_against_allocatable(backend: LiveBackend):
    nodes = backend.get_node_status()
    assert nodes[0]["allocatable"]["memory"] == "8038084Ki"
    assert nodes[0]["allocatedRequests"]["memory"] == "128Mi"
    assert nodes[0]["allocatedRequests"]["cpu"] == "0.1"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("100m", 0.1),
        ("4", 4.0),
        ("128Mi", 134217728.0),
        ("8038084Ki", 8231000576.0),
        ("1Gi", 1073741824.0),
        ("500M", 500000000.0),
        (None, 0.0),
        ("not-a-quantity", 0.0),
    ],
)
def test_quantity_parsing(value, expected):
    assert _quantity(value) == pytest.approx(expected)


def test_only_the_newest_events_are_returned():
    """An hour of earlier incidents on the same workload must not bury the current one or
    exhaust the investigation's token budget."""
    from datetime import timedelta

    from chaosloop.tools.live_backend import MAX_EVENTS

    def event(i: int) -> NS:
        return NS(
            type="Warning", reason="X", count=1, message=f"event {i}",
            last_timestamp=STARTED + timedelta(seconds=i) if i % 2 else None,
            event_time=None if i % 2 else STARTED + timedelta(seconds=i),
            involved_object=NS(name="cart-service-6d4f8797f6-2lcfw", api_version="v1"),
        )

    total = MAX_EVENTS + 40

    class Core(FakeCore):
        def list_namespaced_event(self, namespace, field_selector=None):
            return NS(items=[event(i) for i in reversed(range(total))])

    events = LiveBackend(core_api=Core(), apps_api=FakeApps()).get_events("ns", "cart-service")
    assert events[0] == {"omitted": 40, "note": "40 older events not shown"}
    assert [e["message"] for e in events[1:]] == [f"event {i}" for i in range(40, total)]
    assert all(e["lastTimestamp"] for e in events[1:])  # eventTime stands in when needed


def test_probes_project_with_the_real_client_models():
    """The client calls the exec action `_exec`; a plain stand-in object would hide that."""
    from kubernetes.client import V1ExecAction, V1Probe, V1TCPSocketAction

    from chaosloop.tools.live_backend import _probe

    probe = _probe(V1Probe(_exec=V1ExecAction(command=["cat", "/tmp/ok"])))
    assert probe["exec"] == {"command": ["cat", "/tmp/ok"]}
    tcp = _probe(V1Probe(tcp_socket=V1TCPSocketAction(port=9999)))
    assert tcp["tcpSocket"] == {"port": 9999} and "exec" not in tcp


def test_network_policies_projection_shows_deny_all_and_allowed_peers():

    class Net:
        def list_namespaced_network_policy(self, namespace):
            lockdown = NS(
                metadata=NS(name="cart-ingress-lockdown"),
                spec=NS(
                    pod_selector=NS(match_labels={"app": "cart-service"}),
                    policy_types=["Ingress"],
                    ingress=[],  # deny-all: the signal for network_policy_block
                    egress=None,
                ),
            )
            allow = NS(
                metadata=NS(name="allow-frontend"),
                spec=NS(
                    pod_selector=NS(match_labels={"app": "cart-service"}),
                    policy_types=["Ingress"],
                    ingress=[NS(
                        _from=[NS(
                            pod_selector=NS(match_labels={"app": "checkout-frontend"}),
                            namespace_selector=None, ip_block=None,
                        )],
                        ports=[NS(port=8080, protocol="TCP")],
                    )],
                    egress=None,
                ),
            )
            return NS(items=[lockdown, allow])

    backend = LiveBackend(core_api=FakeCore(), apps_api=FakeApps(), networking_api=Net())
    policies = backend.get_network_policies("chaosloop-lab")

    assert policies[0] == {
        "name": "cart-ingress-lockdown",
        "podSelector": {"app": "cart-service"},
        "policyTypes": ["Ingress"],
        "ingress": [],
        "egress": [],
    }
    assert policies[1]["ingress"][0]["from"][0]["podSelector"] == {"app": "checkout-frontend"}
    assert policies[1]["ingress"][0]["ports"] == [{"port": 8080, "protocol": "TCP"}]


def test_network_policies_unavailable_without_a_networking_client():
    from chaosloop.tools.interface import NOT_AVAILABLE
    assert LiveBackend(core_api=FakeCore(), apps_api=FakeApps()).get_network_policies("ns") == (
        NOT_AVAILABLE
    )
