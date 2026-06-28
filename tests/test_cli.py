import pytest
from typer.testing import CliRunner
from velune.cli.app import create_app
from velune import __version__

runner = CliRunner()

@pytest.fixture
def app():
    return create_app(register="__all__")

def test_version_command(app):
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout

def test_help_command(app):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.stdout

def test_doctor_help(app):
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout

def test_config_help(app):
    result = runner.invoke(app, ["config", "--help"])
    assert result.exit_code == 0

def test_providers_help(app):
    result = runner.invoke(app, ["provider", "--help"])
    assert result.exit_code == 0

def test_models_help(app):
    result = runner.invoke(app, ["models", "--help"])
    assert result.exit_code == 0

def test_invalid_command(app):
    result = runner.invoke(app, ["does-not-exist"])
    assert result.exit_code != 0
    assert "No such command" in result.output
