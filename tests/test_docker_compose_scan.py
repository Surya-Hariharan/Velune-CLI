"""Compose-file risk scan: what `compose_up` must not run unattended (VAIR Decision E)."""

from __future__ import annotations

import pytest

from velune.resources.docker import compose_scan as cs
from velune.resources.docker.compose_scan import compose_risks, scan_compose_text


def _scopes(text: str) -> set[str]:
    return {r.scope for r in scan_compose_text(text)}


BENIGN = """\
name: demo
services:
  api:
    image: python:3.12
    build: .
    command:
      - /usr/bin/server
      - --port=8000
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      - DEBUG=1
      - DATA_DIR=/data
    volumes:
      - ./src:/app/src
      - data:/var/lib/data
      - type: bind
        source: ./config
        target: /etc/app
    network_mode: "service:db"
    privileged: false
    cap_drop:
      - ALL
  db:
    image: postgres:16
volumes:
  data: {}
"""


def test_an_ordinary_project_has_no_risks():
    assert scan_compose_text(BENIGN) == []


@pytest.mark.parametrize(
    ("snippet", "scope"),
    [
        ("    privileged: true", cs.SCOPE_PRIVILEGED),
        ("    privileged: 'true'", cs.SCOPE_PRIVILEGED),
        ("    privileged: ${PRIV}", cs.SCOPE_PRIVILEGED),
        ("    network_mode: host", cs.SCOPE_HOST_NETWORK),
        ('    network_mode: "host"', cs.SCOPE_HOST_NETWORK),
        ("    network_mode: ${NET:-bridge}", cs.SCOPE_HOST_NETWORK),
        ("    pid: host", cs.SCOPE_PRIVILEGED),
        ("    ipc: host", cs.SCOPE_PRIVILEGED),
        ("    userns_mode: host", cs.SCOPE_PRIVILEGED),
        ("    cap_add:\n      - SYS_ADMIN", cs.SCOPE_PRIVILEGED),
        ("    security_opt:\n      - seccomp=unconfined", cs.SCOPE_PRIVILEGED),
        ("    devices:\n      - /dev/sda:/dev/sda", cs.SCOPE_DEVICE),
        ("    device_cgroup_rules:\n      - 'c 1:3 mr'", cs.SCOPE_DEVICE),
        ("    volumes:\n      - /:/host", cs.SCOPE_HOST_MOUNT),
        ("    volumes:\n      - /etc:/etc:ro", cs.SCOPE_HOST_MOUNT),
        ("    volumes:\n      - ~/.ssh:/root/.ssh", cs.SCOPE_HOST_MOUNT),
        ("    volumes:\n      - ../other:/data", cs.SCOPE_HOST_MOUNT),
        ('    volumes:\n      - "C:\\\\Users\\\\me:/data"', cs.SCOPE_HOST_MOUNT),
        ("    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock", cs.SCOPE_HOST_MOUNT),
        ("    volumes:\n      - type: bind\n        source: /srv/data", cs.SCOPE_HOST_MOUNT),
        ("volumes:\n  v:\n    driver_opts:\n      device: /mnt/host", cs.SCOPE_HOST_MOUNT),
        ("include:\n  - other.yml", cs.SCOPE_UNVERIFIED),
        ("    extends:\n      file: base.yml", cs.SCOPE_UNVERIFIED),
    ],
)
def test_risky_constructs_are_flagged(snippet, scope):
    text = f"services:\n  app:\n    image: x\n{snippet}\n"
    assert scope in _scopes(text)


def test_comments_and_falsy_values_are_not_flagged():
    text = """\
services:
  app:
    image: x
    # privileged: true
    privileged: false   # not privileged
    cap_add: []
    network_mode: bridge
"""
    assert scan_compose_text(text) == []


def test_flow_style_values_are_still_seen():
    assert cs.SCOPE_PRIVILEGED in _scopes("services: {a: {image: x}}\n    privileged: true\n")


def test_each_distinct_risk_is_reported_once():
    text = "    privileged: true\n    privileged: true\n    pid: host\n"
    risks = scan_compose_text(text)
    assert len(risks) == len({(r.scope, r.reason) for r in risks}) == 2


# ── Files ───────────────────────────────────────────────────────────────────


def test_default_file_is_discovered_in_the_base_directory(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services:\n  a:\n    privileged: true\n")
    assert {r.scope for r in compose_risks(None, tmp_path)} == {cs.SCOPE_PRIVILEGED}


def test_explicit_file_relative_to_the_base(tmp_path):
    (tmp_path / "dev.yml").write_text("services:\n  a:\n    network_mode: host\n")
    assert {r.scope for r in compose_risks("dev.yml", tmp_path)} == {cs.SCOPE_HOST_NETWORK}


def test_compose_yaml_takes_precedence_like_docker(tmp_path):
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / "docker-compose.yml").write_text("    privileged: true\n")
    assert compose_risks(None, tmp_path) == []


def test_no_compose_file_is_not_a_risk(tmp_path):
    assert compose_risks(None, tmp_path) == []
    assert compose_risks("missing.yml", tmp_path) == []


def test_a_directory_instead_of_a_file_is_unverified(tmp_path):
    (tmp_path / "compose.yml").mkdir()
    assert {r.scope for r in compose_risks("compose.yml", tmp_path)} == {cs.SCOPE_UNVERIFIED}


def test_an_oversized_file_is_unverified(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "MAX_COMPOSE_BYTES", 64)
    (tmp_path / "compose.yml").write_text("services:\n" + "  # padding\n" * 50)
    assert {r.scope for r in compose_risks(None, tmp_path)} == {cs.SCOPE_UNVERIFIED}


def test_an_unreadable_file_is_unverified(tmp_path, monkeypatch):
    (tmp_path / "compose.yml").write_text("services: {}\n")

    def boom(self, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(type(tmp_path / "x"), "open", boom)
    assert {r.scope for r in compose_risks(None, tmp_path)} == {cs.SCOPE_UNVERIFIED}


def test_undecodable_bytes_do_not_crash_the_scan(tmp_path):
    (tmp_path / "compose.yml").write_bytes(b"services:\n  a:\n    privileged: true\n\xff\xfe\x00")
    assert cs.SCOPE_PRIVILEGED in {r.scope for r in compose_risks(None, tmp_path)}
