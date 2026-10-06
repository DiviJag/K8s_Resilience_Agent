import pytest

from chaosloop.live.paths import (
    PathError,
    apply_fragment,
    apply_json_patch,
    assign,
    merge_value,
    split_path,
)

DEPLOYMENT = {
    "metadata": {"name": "billing-service"},
    "spec": {
        "replicas": 2,
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "billing",
                        "image": "localhost:5001/busybox:1.36",
                        "env": [
                            {"name": "LOG_LEVEL", "value": "info"},
                            {"name": "PORT", "value": "8080"},
                        ],
                    }
                ]
            }
        },
    },
}


def test_json_patch_replace_add_remove_without_touching_the_original():
    patched = apply_json_patch(
        DEPLOYMENT,
        [
            {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "x:v9"},
            {"op": "add", "path": "/spec/template/spec/nodeSelector", "value": {"gpu": "t4"}},
            {"op": "remove", "path": "/spec/template/spec/containers/0/env/0"},
            {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "Z"}},
        ],
    )
    container = patched["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "x:v9"
    assert [e["name"] for e in container["env"]] == ["PORT", "Z"]
    assert patched["spec"]["template"]["spec"]["nodeSelector"] == {"gpu": "t4"}
    assert DEPLOYMENT["spec"]["template"]["spec"]["containers"][0]["image"].endswith("1.36")


@pytest.mark.parametrize(
    "ops",
    [
        [{"op": "replace", "path": "/spec/missing", "value": 1}],
        [{"op": "remove", "path": "/spec/nope"}],
        [{"op": "move", "path": "/spec/replicas", "from": "/x"}],
        [{"op": "replace", "path": "", "value": {}}],
    ],
)
def test_json_patch_refuses_what_it_cannot_do_faithfully(ops):
    with pytest.raises(PathError):
        apply_json_patch(DEPLOYMENT, ops)


def test_pointer_escapes_are_honoured():
    doc = {"metadata": {"annotations": {}}}
    out = apply_json_patch(
        doc, [{"op": "add", "path": "/metadata/annotations/chaosloop.io~1run", "value": "r1"}]
    )
    assert out["metadata"]["annotations"] == {"chaosloop.io/run": "r1"}


def test_split_path_reads_both_notations():
    assert split_path("spec.template.spec.containers[0].env") == [
        "spec", "template", "spec", "containers", 0, "env"
    ]
    assert split_path("/spec/ports/0/targetPort") == ["spec", "ports", 0, "targetPort"]


def test_env_fix_adds_without_deleting_other_variables():
    fixed = assign(
        DEPLOYMENT,
        "spec.template.spec.containers[0].env",
        [{"name": "DATABASE_URL", "value": "postgres://db"}],
    )
    env = fixed["spec"]["template"]["spec"]["containers"][0]["env"]
    assert [e["name"] for e in env] == ["LOG_LEVEL", "PORT", "DATABASE_URL"]


def test_named_merge_updates_in_place():
    merged = merge_value(
        [{"name": "PORT", "value": "8080"}], [{"name": "PORT", "value": "9090"}]
    )
    assert merged == [{"name": "PORT", "value": "9090"}]


def test_unindexed_path_into_a_single_container_is_unambiguous():
    fixed = assign(DEPLOYMENT, "spec.template.spec.containers.image", "x:v1")
    assert fixed["spec"]["template"]["spec"]["containers"][0]["image"] == "x:v1"


def test_unindexed_path_into_many_containers_is_refused():
    two = assign(
        DEPLOYMENT,
        "spec.template.spec.containers",
        [{"name": "sidecar", "image": "s:1"}],
    )
    assert len(two["spec"]["template"]["spec"]["containers"]) == 2
    with pytest.raises(PathError, match="without an index"):
        assign(two, "spec.template.spec.containers.image", "x:v1")


def test_fragment_accepts_dotted_and_nested_keys():
    fixed = apply_fragment(
        DEPLOYMENT,
        {
            "spec.template.spec.imagePullSecrets": [{"name": "regcred"}],
            "spec": {"template": {"spec": {"tolerations": [{"key": "chaosloop"}]}}},
        },
    )
    pod = fixed["spec"]["template"]["spec"]
    assert pod["imagePullSecrets"] == [{"name": "regcred"}]
    assert pod["tolerations"] == [{"key": "chaosloop"}]
    assert pod["containers"][0]["name"] == "billing"  # nested merge kept the rest


def test_an_empty_value_clears_rather_than_merging_nothing():
    selected = assign(DEPLOYMENT, "spec.template.spec.nodeSelector", {"gpu": "t4"})
    cleared = assign(selected, "spec.template.spec.nodeSelector", {})
    assert cleared["spec"]["template"]["spec"]["nodeSelector"] == {}
    no_env = assign(DEPLOYMENT, "spec.template.spec.containers[0].env", [])
    assert no_env["spec"]["template"]["spec"]["containers"][0]["env"] == []


def test_assign_creates_missing_mappings():
    fixed = assign({"spec": {}}, "spec.template.spec.nodeSelector", {})
    assert fixed == {"spec": {"template": {"spec": {"nodeSelector": {}}}}}
