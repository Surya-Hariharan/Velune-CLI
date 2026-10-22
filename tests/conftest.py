"""Suite-wide fixtures.

``ProviderRegistry`` publishes the endpoints it resolves (Ollama host, cloud
base URLs) to :mod:`velune.providers.endpoints` so validation and discovery use
the same ones. That is process-wide state; reset it after every test so one
test's custom base URL cannot redirect another test's requests.
"""

from __future__ import annotations

import importlib.util

import pytest

# The real-terminal suite needs pyte to read the emulated screen back. CI jobs that
# install only the core test tooling (the lowest-dependency-versions matrix) cannot
# even import it, so skip collecting it there instead of erroring.
collect_ignore_glob = [] if importlib.util.find_spec("pyte") else ["tui/*"]


@pytest.fixture(autouse=True)
def _reset_published_endpoints():
    yield
    from velune.providers import endpoints

    endpoints.configure_ollama_url(None)
    for provider_id in list(endpoints.DEFAULT_BASE_URLS):
        endpoints.configure_base_url(provider_id, None)
