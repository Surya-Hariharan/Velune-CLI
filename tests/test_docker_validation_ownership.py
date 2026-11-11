"""Docker argument validation and the ownership store (VAIR A4)."""

from __future__ import annotations

import json

import pytest

from velune.resources.docker import validation as v
from velune.resources.docker.ownership import (
    LABEL_MANAGED,
    LABEL_SESSION,
    LABEL_WORKSPACE,
    DockerOwnership,
    DockerOwnershipStore,
    Facts,
)

# ── Validation ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ok", ["web", "web-1", "my_app.v2", "a" * 128, "3f2a9c1b7d4e"])
def test_container_references_that_are_names_or_ids_are_accepted(ok):
    assert v.container_ref(ok) == ok


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ",
        "-f",
        "--all",
        "-",
        "a b",
        "a/b",
        "../x",
        "a;rm -rf",
        "a$(x)",
        "a\x00b",
        "a" * 129,
        "/etc",
        "é",
        None,
        5,
    ],
)
def test_container_references_cannot_be_options_paths_or_shell_text(bad):
    with pytest.raises(v.DockerValidationError):
        v.container_ref(bad)


@pytest.mark.parametrize(
    "ok",
    [
        "nginx",
        "nginx:1.27",
        "library/nginx:latest",
        "ghcr.io/org/app:v1.2",
        "localhost:5000/app:dev",
        "registry.example.com:8443/team/svc@sha256:" + "a" * 64,
        "a_b/c-d.e:tag_1",
    ],
)
def test_valid_image_references(ok):
    assert v.image_ref(ok) == ok


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "-x",
        "--rm",
        "NGINX",
        "nginx:",
        "nginx:bad tag",
        "a b",
        "http://x/y",
        "x@sha256:short",
        "../x",
        "x" * 300,
        "x;y",
    ],
)
def test_invalid_image_references(bad):
    with pytest.raises(v.DockerValidationError):
        v.image_ref(bad)


def test_image_references_are_split_and_attributed_to_a_registry():
    assert v.split_image("nginx") == ("nginx", "latest")
    assert v.split_image("ghcr.io/org/app:v1") == ("ghcr.io/org/app", "v1")
    assert v.registry_of("nginx:1") == "docker.io" and v.registry_of("library/nginx") == "docker.io"
    assert (
        v.registry_of("ghcr.io/org/app") == "ghcr.io"
        and v.registry_of("localhost:5000/a") == "localhost:5000"
    )


def test_commands_are_split_like_a_shell_but_never_run_by_one():
    assert v.command_args("sh -c 'echo hi there'") == ["sh", "-c", "echo hi there"]
    for bad in ["", "echo 'unbalanced", "x " * 100]:
        with pytest.raises(v.DockerValidationError):
            v.command_args(bad)


def test_environment_variables():
    assert v.env_pairs(["A=1", "B_2=two words", "C="]) == {"A": "1", "B_2": "two words", "C": ""}
    for bad in (
        ["nope"],
        ["1A=x"],
        ["A B=x"],
        ["=x"],
        "A=1",
        ["A=" + "x" * 5000],
        [f"V{i}=x" for i in range(100)],
    ):
        with pytest.raises(v.DockerValidationError):
            v.env_pairs(bad)


def test_ports_bind_to_loopback_by_default_and_flag_exposure():
    local = v.port_bindings(["8080:80", "127.0.0.1:5432:5432/tcp", "[::1]:9000:90"])
    assert [(b.host_ip, b.host_port, b.container_port) for b in local] == [
        ("127.0.0.1", 8080, 80),
        ("127.0.0.1", 5432, 5432),
        ("::1", 9000, 90),
    ]
    assert not any(b.exposed for b in local)
    exposed = v.port_bindings(["0.0.0.0:80:80", "192.168.1.5:81:81/udp"])
    assert all(b.exposed for b in exposed) and exposed[1].protocol == "udp"
    for bad in (
        ["80"],
        ["a:b"],
        ["70000:80"],
        ["0:80"],
        ["1.2.3:80:80"],
        ["80:80/icmp"],
        ["x" * 50],
    ):
        with pytest.raises(v.DockerValidationError):
            v.port_bindings(bad)


def test_mounts_are_classified(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / ".env").write_text("K=v")
    specs = v.mount_specs(
        [
            "data:/var/lib/data",
            "./src:/app:ro",
            f"{tmp_path / '.env'}:/secret",
            "/etc:/host-etc",
            "/var/run/docker.sock:/var/run/docker.sock",
        ],
        tmp_path,
    )
    named, project, secret, outside, sock = specs
    assert (named.kind, named.source, named.read_only) == ("volume", "data", False)
    assert (
        project.kind == "bind"
        and project.inside_workspace
        and project.read_only
        and not project.secret
    )
    assert secret.inside_workspace and secret.secret
    assert outside.kind == "bind" and not outside.inside_workspace
    assert sock.docker_socket and not sock.inside_workspace


def test_a_mount_that_escapes_the_workspace_through_dotdot_is_outside(tmp_path):
    (tmp_path / "ws").mkdir()
    spec = v.mount_specs(["../:/x"], tmp_path / "ws")[0]
    assert not spec.inside_workspace


@pytest.mark.parametrize(
    "bad",
    [
        "justone",
        "src:relative/path",
        "src:/a/../b",
        ":/x",
        "bad name:/x",
        "x:/a:/b:rw",
        ["list"],
        "src:/a//b",
    ],
)
def test_malformed_mounts_are_refused(bad, tmp_path):
    with pytest.raises(v.DockerValidationError):
        v.mount_specs([bad] if isinstance(bad, str) else bad, tmp_path)


def test_labels_cannot_forge_ownership_or_compose_membership():
    assert v.label_pairs(["env=dev", "team.name=core"]) == {"env": "dev", "team.name": "core"}
    for bad in (
        ["dev.velune.managed=true"],
        ["com.docker.compose.project=x"],
        ["DEV.VELUNE.MANAGED=true"],
        ["noequals"],
        ["Bad Key=x"],
    ):
        with pytest.raises(v.DockerValidationError):
            v.label_pairs(bad)


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("512m", True),
        ("2g", True),
        ("1024", False),
        ("1m", False),
        ("12x", False),
        ("-5m", False),
        ("9999g", False),
    ],
)
def test_memory_limits(text, ok):
    if ok:
        assert v.memory_bytes(text) > 0
    else:
        with pytest.raises(v.DockerValidationError):
            v.memory_bytes(text)


def test_cpu_devices_and_capabilities():
    assert v.cpu_nanos(1.5) == 1_500_000_000
    for bad in (0, 1000, "x"):
        with pytest.raises(v.DockerValidationError):
            v.cpu_nanos(bad)
    assert v.device_specs(["/dev/ttyUSB0", "/dev/sda:/dev/xvda"]) == [
        "/dev/ttyUSB0",
        "/dev/sda:/dev/xvda",
    ]
    for bad in (["/etc/passwd"], ["/dev/../etc"], ["dev/x"]):
        with pytest.raises(v.DockerValidationError):
            v.device_specs(bad)
    assert v.capabilities(["net_admin"]) == ["NET_ADMIN"]
    with pytest.raises(v.DockerValidationError):
        v.capabilities(["NET ADMIN"])


# ── Ownership store ─────────────────────────────────────────────────────────


def store(tmp_path, session="s1", daemon="D1", file=True):
    s = DockerOwnershipStore(
        session, tmp_path, path=tmp_path / "state" / "own.json" if file else None
    )
    s.bind_daemon(daemon)
    return s


def facts(kind="container", ident="c1", fingerprint="t0", labels=None, name="web"):
    return Facts(kind, ident, name, fingerprint, labels or {})


def labelled(s, **kw):
    return facts(labels=s.labels(), **kw)


def test_an_object_velune_created_is_owned_by_this_session(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s))
    assert s.classify(labelled(s)) is DockerOwnership.VELUNE and DockerOwnership.VELUNE.trusted


def test_a_name_match_alone_never_confers_ownership(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s, ident="c1", name="web"))
    other = labelled(s, ident="c2", name="web")  # a different object with the same name
    assert s.classify(other) is DockerOwnership.EXTERNAL  # claims Velune labels but has no record
    assert s.classify(facts(ident="c3", name="web")) is DockerOwnership.UNKNOWN


def test_labels_are_claims_not_proof(tmp_path):
    s = store(tmp_path)
    forged = facts(
        ident="evil",
        labels={LABEL_MANAGED: "true", LABEL_SESSION: "s1", LABEL_WORKSPACE: s.workspace_hash},
    )
    assert s.classify(forged) is DockerOwnership.EXTERNAL and not s.classify(forged).trusted


def test_a_record_whose_object_changed_is_not_trusted(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s, fingerprint="t0"))
    assert (
        s.classify(labelled(s, fingerprint="t1")) is DockerOwnership.UNKNOWN
    )  # same id, different object
    stripped = facts(labels={})  # the labels were removed
    assert s.classify(stripped) is DockerOwnership.UNKNOWN  # inconsistent: fail safe
    tampered = facts(labels={**s.labels(), LABEL_WORKSPACE: "other-workspace"})
    assert s.classify(tampered) is DockerOwnership.UNKNOWN


def test_unlookupable_objects_are_unknown(tmp_path):
    assert store(tmp_path).classify(None) is DockerOwnership.UNKNOWN


def test_objects_present_at_connect_are_preexisting(tmp_path):
    s = store(tmp_path)
    s.remember_existing("container", ["old1"])
    assert s.classify(facts(ident="old1")) is DockerOwnership.PREEXISTING
    assert s.classify(facts(ident="new")) is DockerOwnership.UNKNOWN
    assert not DockerOwnership.PREEXISTING.trusted


def test_compose_labels_alone_are_external(tmp_path):
    s = store(tmp_path)
    assert (
        s.classify(facts(labels={"com.docker.compose.project": "app"})) is DockerOwnership.EXTERNAL
    )


def test_images_are_owned_by_id_without_label_checks(tmp_path):
    s = store(tmp_path)
    img = facts("image", "sha256:abc", fingerprint="sha256:abc", name="app:dev")
    s.record_created(img)
    assert s.classify(img) is DockerOwnership.VELUNE
    assert (
        s.classify(facts("image", "sha256:other", fingerprint="sha256:other"))
        is DockerOwnership.UNKNOWN
    )


def test_compose_created_objects_are_proven_by_their_project_label(tmp_path):
    s = store(tmp_path)
    s.record_created(facts(ident="c9", labels={"com.docker.compose.project": "app"}), project="app")
    assert (
        s.classify(facts(ident="c9", labels={"com.docker.compose.project": "app"}))
        is DockerOwnership.VELUNE
    )
    assert (
        s.classify(facts(ident="c9", labels={"com.docker.compose.project": "other"}))
        is DockerOwnership.UNKNOWN
    )


def test_ownership_survives_a_restart_as_previous_session(tmp_path):
    first = store(tmp_path, session="s1")
    first.record_created(labelled(first))
    second = store(tmp_path, session="s2")  # a new process, same workspace and daemon
    seen = labelled(first)
    assert second.classify(seen) is DockerOwnership.PREVIOUS
    assert not DockerOwnership.PREVIOUS.trusted  # needs confirmation, but cleanup is possible


def test_records_belong_to_one_daemon(tmp_path):
    local = store(tmp_path, daemon="LOCAL")
    local.record_created(labelled(local))
    remote = store(tmp_path, session="s1", daemon="REMOTE")
    assert (
        remote.classify(labelled(remote)) is DockerOwnership.EXTERNAL
    )  # same ids mean nothing elsewhere


def test_a_corrupt_record_file_trusts_nothing(tmp_path):
    first = store(tmp_path)
    first.record_created(labelled(first))
    (tmp_path / "state" / "own.json").write_text("{not json", encoding="utf-8")
    again = store(tmp_path, session="s1")
    assert again.classify(labelled(again)) is DockerOwnership.EXTERNAL


def test_release_forgets_an_object(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s))
    s.release("container", "c1")
    assert s.classify(labelled(s)) is DockerOwnership.EXTERNAL
    assert json.loads((tmp_path / "state" / "own.json").read_text())["daemons"]["D1"]["records"] == []


def test_reconcile_drops_missing_changed_and_relabelled_records(tmp_path):
    s = store(tmp_path)
    for ident in ("gone", "changed", "relabelled", "fine"):
        s.record_created(labelled(s, ident=ident))
    live = {
        "container": {
            "changed": labelled(s, ident="changed", fingerprint="NEW"),
            "relabelled": facts(ident="relabelled", labels={}),
            "fine": labelled(s, ident="fine"),
        }
    }
    report = s.reconcile(live)
    assert report == {
        "missing": ["container:gone"],
        "changed": ["container:changed"],
        "relabelled": ["container:relabelled"],
    }
    assert [r.id for r in s.owned("container")] == ["fine"]


def test_reconcile_leaves_unlisted_kinds_alone(tmp_path):
    s = store(tmp_path)
    s.record_created(labelled(s, kind="volume", ident="vol"))
    assert s.reconcile({"container": {}}) == {"missing": [], "changed": [], "relabelled": []}
    assert len(s.owned("volume")) == 1  # an unreadable listing must not erase everything


def test_the_store_works_without_a_file(tmp_path):
    s = store(tmp_path, file=False)
    s.record_created(labelled(s))
    assert s.classify(labelled(s)) is DockerOwnership.VELUNE
