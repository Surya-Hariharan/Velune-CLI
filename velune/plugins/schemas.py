"""Plugin manifest schemas and validation."""

from __future__ import annotations

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class PluginManifest(BaseModel):
    """Manifest data loaded from a plugin directory containing metadata and registration hooks."""

    name: str = Field(..., description="Unique alphabetic slug of the plugin.")
    version: str = Field("0.1.0", description="Plugin semantic version.")
    description: str = Field("", description="Short plugin functional details.")
    entry_point: str = Field(..., description="Relative python module path (e.g., 'plugin.py').")
    hooks: List[str] = Field(default_factory=list, description="List of Hook names subscribed (e.g., 'pre_execute').")
    author: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
