import pytest
import httpx
import asyncio

def pytest_configure(config):
    config.addinivalue_line("markers", "ollama: marks tests that require Ollama running")
    config.addinivalue_line("markers", "slow: marks slow-running tests")

@pytest.fixture(scope="session")
def ollama_available() -> bool:
    """Check if Ollama is running and has at least one model."""
    try:
        response = httpx.get("http://localhost:11434/api/tags", timeout=3.0)
        models = response.json().get("models", [])
        return len(models) > 0
    except Exception:
        return False

@pytest.fixture(scope="session")
def ollama_model(ollama_available) -> str:
    """Return the first available Ollama model name."""
    if not ollama_available:
        pytest.skip("Ollama not available")
    response = httpx.get("http://localhost:11434/api/tags", timeout=3.0)
    models = response.json().get("models", [])
    return models[0]["name"]

@pytest.fixture(scope="session")  
def skip_without_ollama(ollama_available):
    if not ollama_available:
        pytest.skip("Ollama not available - skipping integration test")
