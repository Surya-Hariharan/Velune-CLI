"""Synthetic repository corpus for Repository Intelligence regression tests.

These eight repo shapes mirror the baseline benchmark corpus described in
``docs/REPOSITORY_INTELLIGENCE_BASELINE.md`` (Appendix A): well-structured,
legacy PHP, notebook-first ML, a training pipeline with a vendored file,
a JS/Python monorepo with nested-only manifests, terse/single-letter naming,
dynamic-import-based plugin loading, and a Rust+Python polyglot repo.

Each builder function materializes one style under a given root directory.
Tests import ``materialize(style, tmp_path)`` to get a ready-to-index
workspace, then assert against the classifiers in ``velune/repository/``.
Some assertions here pin *known, documented* gaps (e.g. notebooks are
invisible to discovery, dynamic imports aren't resolved) rather than ideal
behavior — those are intentional characterization tests: they lock in the
current, benchmarked state so a future fix is a deliberate, visible test
change rather than a silent behavior drift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

STYLES: tuple[str, ...] = (
    "well_structured",
    "legacy_php_style",
    "notebook_ml",
    "ml_pipeline",
    "monorepo",
    "poorly_named",
    "dynamic_imports",
    "mixed_lang_polyglot",
)


def _write(root: Path, rel: str, content: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _build_well_structured(root: Path) -> None:
    _write(
        root,
        "pyproject.toml",
        '[project]\nname = "shop-api"\nversion = "0.1.0"\n'
        'dependencies = ["fastapi", "uvicorn", "sqlalchemy", "pydantic"]\n'
        "[tool.pytest.ini_options]\naddopts = \"-q\"\n[tool.ruff]\nline-length = 100\n",
    )
    _write(
        root,
        "app/main.py",
        "from fastapi import FastAPI\nfrom app.routers import items\n\n"
        'app = FastAPI()\napp.include_router(items.router)\n',
    )
    _write(
        root,
        "app/routers/items.py",
        "from fastapi import APIRouter\nfrom app.services.item_service import get_item\n\n"
        'router = APIRouter(prefix="/items")\n\n\n'
        '@router.get("/{item_id}")\ndef read_item(item_id: int):\n    return get_item(item_id)\n',
    )
    _write(
        root,
        "app/models/item.py",
        "from sqlalchemy import Column, Integer, String\nfrom sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\nclass Item(Base):\n"
        '    __tablename__ = "items"\n    id = Column(Integer, primary_key=True)\n'
        "    name = Column(String)\n",
    )
    _write(
        root,
        "app/services/item_service.py",
        "from app.models.item import Item\n\n\ndef get_item(item_id: int) -> dict:\n"
        '    return {"id": item_id, "name": "widget"}\n',
    )
    _write(root, ".env", "DATABASE_URL=postgres://user:hunter2@localhost/db\n")
    _write(
        root,
        "tests/test_items.py",
        "def test_placeholder():\n    assert True\n",
    )


def _build_legacy_php_style(root: Path) -> None:
    _write(
        root,
        "index.php",
        "<?php\nrequire_once 'includes/db.php';\nrequire_once 'includes/auth.php';\n"
        "echo greet();\n",
    )
    _write(
        root,
        "includes/db.php",
        "<?php\nfunction get_connection() {\n    return mysqli_connect('localhost', 'root', '');\n}\n",
    )
    _write(
        root,
        "includes/auth.php",
        "<?php\nfunction greet() {\n    return 'hello';\n}\n",
    )
    _write(
        root,
        "assets/app.js",
        "var greeting = 'hi';\nfunction showGreeting() {\n"
        "    document.write(greeting);\n}\n",
    )


def _build_notebook_ml(root: Path) -> None:
    notebook = {
        "cells": [
            {
                "cell_type": "code",
                "execution_count": 1,
                "source": ["import pandas as pd\n", "df = pd.read_csv('data.csv')\n"],
                "outputs": [],
            },
            {
                "cell_type": "code",
                "execution_count": 2,
                "source": ["def train():\n", "    return df.mean()\n"],
                "outputs": [],
            },
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    _write(root, "notebooks/train.ipynb", json.dumps(notebook))
    _write(root, "notebooks/explore.ipynb", json.dumps(notebook))
    _write(
        root,
        "src/helpers.py",
        "def normalize(values):\n    total = sum(values)\n    return [v / total for v in values]\n",
    )


def _build_ml_pipeline(root: Path, vendored_functions: int = 15000) -> None:
    _write(
        root,
        "train.py",
        "from preprocess import clean\nfrom model import Model\n\n\ndef train():\n"
        "    data = clean([1, 2, 3])\n    return Model().fit(data)\n",
    )
    _write(root, "preprocess.py", "def clean(data):\n    return [d for d in data if d is not None]\n")
    _write(root, "model.py", "class Model:\n    def fit(self, data):\n        return data\n")
    vendored_lines = [f"def vendored_fn_{i}():\n    return {i}\n\n" for i in range(vendored_functions)]
    # Deliberately NOT under vendor/ — that directory name is now excluded at
    # discovery time (see scanner.py), so a file there would never reach the
    # parser at all. This path exercises the separate, independent size guard
    # (MAX_STRUCTURAL_PARSE_BYTES) that protects against an oversized file in
    # a directory name the scanner doesn't otherwise recognize.
    _write(root, "generated_output/big_generated.py", "".join(vendored_lines))


def _build_monorepo(root: Path) -> None:
    _write(
        root,
        "package.json",
        '{"name": "monorepo-root", "private": true, "workspaces": ["packages/*"]}\n',
    )
    _write(
        root,
        "packages/frontend/src/api.ts",
        "export async function loadItems() {\n"
        '  const res = await fetch("/items");\n  return res.json();\n}\n',
    )
    _write(
        root,
        "packages/backend-api/main.py",
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        '@app.get("/items")\ndef list_items():\n    return []\n',
    )


def _build_poorly_named(root: Path) -> None:
    _write(root, "a/b.py", "def f():\n    return 1\n")
    _write(root, "c/d.py", "def g():\n    return 2\n")
    _write(root, "e/f.py", "def h():\n    return 3\n")
    _write(root, "main.py", "from a.b import f\n\nprint(f())\n")


def _build_dynamic_imports(root: Path) -> None:
    _write(
        root,
        "loader.py",
        "import importlib\n\n\ndef load_plugin(name: str):\n"
        '    module = importlib.import_module(f"plugins.{name}")\n    return module\n',
    )
    _write(root, "plugins/__init__.py", "")
    _write(root, "plugins/plugin_a.py", "def run():\n    return 'a'\n")
    _write(root, "plugins/plugin_b.py", "def run():\n    return 'b'\n")
    _write(
        root,
        "app.js",
        "function loadModule(modPath) {\n  const mod = require(modPath);\n  return mod;\n}\n"
        "module.exports = { loadModule };\n",
    )
    _write(root, "modules/dynamic_mod.js", "module.exports = { greet: () => 'hi' };\n")


def _build_mixed_lang_polyglot(root: Path) -> None:
    _write(
        root,
        "Cargo.toml",
        '[package]\nname = "core"\nversion = "0.1.0"\nedition = "2021"\n',
    )
    _write(
        root,
        "src/lib.rs",
        "pub fn add(a: i64, b: i64) -> i64 {\n    a + b\n}\n",
    )
    _write(root, "pyproject.toml", '[project]\nname = "wrapper"\nversion = "0.1.0"\n')
    _write(
        root,
        "wrapper.py",
        "def call_core():\n    return 'delegates to the Rust core via FFI'\n",
    )


_BUILDERS: dict[str, Callable[[Path], None]] = {
    "well_structured": _build_well_structured,
    "legacy_php_style": _build_legacy_php_style,
    "notebook_ml": _build_notebook_ml,
    "ml_pipeline": _build_ml_pipeline,
    "monorepo": _build_monorepo,
    "poorly_named": _build_poorly_named,
    "dynamic_imports": _build_dynamic_imports,
    "mixed_lang_polyglot": _build_mixed_lang_polyglot,
}


def materialize(style: str, root: Path) -> Path:
    """Write the given repo style's files under *root* and return it."""
    if style not in _BUILDERS:
        raise ValueError(f"Unknown repo style: {style!r} (known: {sorted(_BUILDERS)})")
    _BUILDERS[style](root)
    return root
