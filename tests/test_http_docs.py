"""The HTTP documentation's examples are real: configuration and collections parse."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from velune.kernel.config import HttpConfig, ResourcesConfig
from velune.resources.http.collections import parse_collection
from velune.resources.http.environments import Environment

DOC = Path(__file__).resolve().parent.parent / "docs" / "http.md"


def blocks(language: str) -> list[str]:
    text = DOC.read_text(encoding="utf-8")
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


def toml_loads(text: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    return tomllib.loads(text)


def test_every_toml_example_is_valid_http_configuration():
    found = blocks("toml")
    assert len(found) >= 2
    for text in found:
        document = toml_loads(text)
        config = HttpConfig.model_validate(document["resources"]["http"])
        for name, env in config.environments.items():
            Environment.from_config(name, env)  # the documented environments validate


def test_the_reference_example_lists_every_setting():
    reference = next(t for t in blocks("toml") if "max_collection_requests" in t)
    documented = set(toml_loads(reference)["resources"]["http"]) - {"environments"}
    assert documented == set(HttpConfig.model_fields) - {"environments", "auto_connect"}


def test_the_documented_collection_is_a_valid_collection():
    example = next(b for b in blocks("json") if "velune-api-collection/1" in b)
    collection = parse_collection(json.loads(example))
    assert [r.id for r in collection.requests] == ["login", "me"]


def test_the_default_configuration_ships_an_empty_http_section():
    http = ResourcesConfig().http
    assert http.enabled and not http.auto_connect and http.trusted_origins == []
    assert http.allow_loopback and not http.allow_private_network


@pytest.mark.parametrize(
    "bad",
    [
        {"connect_timeout": 0},
        {"timeout": 9999},
        {"max_redirects": 99},
        {"max_response_bytes": 10},
        {"max_connections": 0},
        {"max_collection_requests": 0},
    ],
)
def test_out_of_range_settings_are_rejected(bad):
    with pytest.raises(ValidationError):
        HttpConfig.model_validate(bad)
