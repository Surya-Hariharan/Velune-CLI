"""Adapters that bind the orchestration engine's ports to the rest of Velune.

Nothing here is imported by ``velune.orchestration.engine``: the engine package stays free of provider,
tool, permission and CLI code, and these adapters are loaded only by code that asks for them.
"""
