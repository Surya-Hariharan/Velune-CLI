"""Public CLI entry point for Velune."""

from velune.cli.app import app

__all__ = ["app"]


if __name__ == "__main__":
    app()
