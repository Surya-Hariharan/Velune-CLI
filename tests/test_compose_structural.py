"""The Compose risk scan reads the document, not just its lines (VAIR A5)."""

from __future__ import annotations

import builtins

import pytest

from velune.resources.docker import compose_scan as cs
from velune.resources.docker.compose_scan import scan_compose_text

yaml = pytest.importorskip("yaml")


def scopes(text: str) -> set[str]:
    return {r.scope for r in scan_compose_text(text)}


def svc(body: str) -> str:
    indented = "".join(f"    {line}\n" for line in body.strip().splitlines())
    return f"services:\n  web:\n    image: x\n{indented}"


# ── What a line scan cannot see ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "scope"),
    [
        ("services: {web: {image: x, privileged: true}}\n", cs.SCOPE_PRIVILEGED),
        ('{"services": {"web": {"image": "x", "privileged": true}}}', cs.SCOPE_PRIVILEGED),
        (svc('"privileged": true'), cs.SCOPE_PRIVILEGED),
        (svc("privileged: 'yes'"), cs.SCOPE_PRIVILEGED),
        (svc("privileged: !!bool true"), cs.SCOPE_PRIVILEGED),
        (svc("privileged: ${PRIV}"), cs.SCOPE_PRIVILEGED),
        (svc('volumes: ["/:/host"]'), cs.SCOPE_HOST_MOUNT),
        (svc("volumes: ['/var/run/docker.sock:/s']"), cs.SCOPE_HOST_MOUNT),
        (svc("volumes:\n  - ${HOME}:/home"), cs.SCOPE_HOST_MOUNT),
        (svc("volumes:\n  - $PWD/../x:/x"), cs.SCOPE_HOST_MOUNT),
        (svc("volumes:\n  - type: bind\n    source: /etc\n    target: /e"), cs.SCOPE_HOST_MOUNT),
        (svc("volumes:\n  - {type: bind, source: ~/.ssh, target: /k}"), cs.SCOPE_HOST_MOUNT),
        (svc("network_mode: 'host'"), cs.SCOPE_HOST_NETWORK),
        (svc("network_mode: ${MODE}"), cs.SCOPE_HOST_NETWORK),
        (svc("cap_add: [SYS_ADMIN]"), cs.SCOPE_PRIVILEGED),
        (svc("security_opt: ['seccomp=unconfined']"), cs.SCOPE_PRIVILEGED),
        (svc("devices: ['/dev/sda:/dev/sda']"), cs.SCOPE_DEVICE),
        (svc("gpus: all"), cs.SCOPE_DEVICE),
        (svc("pid: host"), cs.SCOPE_PRIVILEGED),
        (svc("ipc: 'host'"), cs.SCOPE_PRIVILEGED),
        (svc("userns_mode: host"), cs.SCOPE_PRIVILEGED),
        (svc("volumes_from: [other]"), cs.SCOPE_EXTERNAL),
        (svc("network_mode: container:abc"), cs.SCOPE_PRIVILEGED),
        (svc("build: ../.."), cs.SCOPE_HOST_MOUNT),
        (svc("build:\n  context: /srv"), cs.SCOPE_HOST_MOUNT),
        (svc("extends:\n  file: other.yml\n  service: base"), cs.SCOPE_UNVERIFIED),
        ("include:\n  - other.yml\nservices: {}\n", cs.SCOPE_UNVERIFIED),
        (
            "services:\n  web:\n    image: x\nvolumes:\n  v:\n    external: true\n",
            cs.SCOPE_EXTERNAL,
        ),
        (
            "services:\n  web:\n    image: x\nnetworks:\n  n:\n    external: true\n",
            cs.SCOPE_EXTERNAL,
        ),
        (
            "services:\n  web:\n    image: x\nvolumes:\n  v:\n    driver_opts:\n      device: /srv/data\n      o: bind\n",
            cs.SCOPE_HOST_MOUNT,
        ),
        (
            "services:\n  web:\n    image: x\nsecrets:\n  s:\n    file: /etc/shadow\n",
            cs.SCOPE_HOST_MOUNT,
        ),
    ],
)
def test_the_structure_reveals_what_a_line_scan_would_miss(text, scope):
    assert scope in scopes(text)


def test_anchors_and_merge_keys_are_resolved():
    text = (
        "x-common: &common\n  privileged: true\nservices:\n  web:\n    <<: *common\n    image: x\n"
    )
    assert cs.SCOPE_PRIVILEGED in scopes(text)
    quiet = "x-common: &c\n  restart: always\nservices:\n  web:\n    <<: *c\n    image: x\n"
    assert scopes(quiet) == set()


def test_every_document_in_a_multi_document_file_is_scanned():
    text = "services:\n  a:\n    image: x\n---\nservices:\n  b:\n    privileged: true\n"
    assert cs.SCOPE_PRIVILEGED in scopes(text)


# ── Ordinary projects stay quiet ────────────────────────────────────────────


def test_a_typical_project_is_clean():
    text = (
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    ports: ['8080:80']\n"
        "    environment:\n      DEBUG: '1'\n      MODE: production\n"
        "    volumes:\n      - ./src:/app\n      - data:/var/lib/data\n"
        "    network_mode: service:db\n"
        "    depends_on: [db]\n"
        "  db:\n    image: postgres:16\n    volumes: [pgdata:/var/lib/postgresql/data]\n"
        "volumes:\n  data: {}\n  pgdata:\n"
        "networks:\n  default:\n    driver: bridge\n"
    )
    assert scan_compose_text(text) == []


def test_the_line_scan_is_kept_as_a_second_opinion_and_errs_toward_asking():
    # An environment variable that happens to be spelled like a Compose key is flagged
    # too: a false positive costs one confirmation, a false negative a privileged container.
    assert cs.SCOPE_PRIVILEGED in scopes(svc("environment:\n  privileged: 'true'"))


# ── What cannot be inspected is reported, never waved through ───────────────


def test_a_file_that_does_not_parse_is_unverified():
    assert cs.SCOPE_UNVERIFIED in scopes("services: [unclosed\n  - x: {")


def test_a_deeply_nested_or_huge_document_is_unverified_not_walked():
    nest = "services:\n  web:\n    image: x\n    labels:\n" + "".join(
        "  " * (3 + i) + f"k{i}:\n" for i in range(60)
    )
    assert cs.SCOPE_UNVERIFIED in scopes(nest + "  " * 63 + "leaf: 1\n")
    many_docs = "---\n".join("a: 1\n" for _ in range(20))
    assert cs.SCOPE_UNVERIFIED in scopes(many_docs)


def test_an_anchor_bomb_is_bounded():
    lines = ["a: &a [x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 12):
        prev = chr(ord("a") + i - 1)
        cur = chr(ord("a") + i)
        lines.append(f"{cur}: &{cur} [{', '.join(['*' + prev] * 9)}]")
    text = "\n".join(lines) + "\nservices:\n  web:\n    image: x\n"
    scan_compose_text(text)  # returns promptly; the walk is bounded, no expansion


def test_the_scan_never_raises_on_hostile_text():
    for text in (
        "",
        "\x00",
        ":::",
        "- - - -",
        "{" * 500,
        "a: " + "[" * 500,
        "&a *a",
        "!!python/object/apply:os.system ['x']",
    ):
        scan_compose_text(text)


def test_python_object_tags_are_never_constructed():
    marker = []
    text = "services:\n  web:\n    image: !!python/object/apply:builtins.exec ['import os']\n"
    assert cs.SCOPE_UNVERIFIED in scopes(text) and marker == []  # safe_load refuses the tag


# ── Without PyYAML: fail closed on what the line scan cannot read ───────────


@pytest.fixture
def no_yaml(monkeypatch):
    real = builtins.__import__

    def deny(name, *a, **k):
        if name == "yaml":
            raise ImportError("no yaml")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", deny)


@pytest.mark.parametrize(
    "text",
    [
        "services: {web: {image: x, privileged: true}}\n",
        svc('"privileged": true'),
        "x-c: &c\n  privileged: true\nservices:\n  web:\n    <<: *c\n    image: x\n",
        "services:\n  web:\n    image: !!str x\n",
    ],
)
def test_without_pyyaml_unreadable_syntax_is_reported_unverified(no_yaml, text):
    assert cs.SCOPE_UNVERIFIED in scopes(text)


def test_without_pyyaml_flow_lists_are_still_checked_by_the_line_scan(no_yaml):
    assert cs.SCOPE_HOST_MOUNT in scopes(svc('volumes: ["/:/host"]'))
    assert cs.SCOPE_HOST_MOUNT in scopes(svc("volumes: [${HOME}:/h]"))


def test_without_pyyaml_an_ordinary_file_is_still_clean(no_yaml):
    text = "services:\n  web:\n    image: x\n    ports: ['8080:80']\n    volumes:\n      - ./a:/b\n"
    assert scan_compose_text(text) == []
    assert cs.SCOPE_PRIVILEGED in scopes(svc("privileged: true"))
