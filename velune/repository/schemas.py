"""Strictly-typed schemas for repository cognition."""

import hashlib
from typing import Any

from pydantic import BaseModel, Field, model_validator

from velune._compat import StrEnum


def build_qualified_name(file_path: str, name: str, parent: str | None = None) -> str:
    """Builds a dotted qualified name for a symbol from its file path and parent scope."""
    p = file_path.replace("\\", "/")

    # Extract package path relative to 'velune/' if absolute
    if "/velune/" in p:
        p = p.split("/velune/", 1)[1]
        p = "velune/" + p
    elif p.startswith("c:") or p.startswith("C:") or ":" in p:
        p = p.split(":", 1)[1].lstrip("/")

    p = p.rsplit(".", 1)[0]
    dotted = p.replace("/", ".").strip(".")

    if parent:
        return f"{dotted}.{parent}.{name}"
    return f"{dotted}.{name}"


def compute_symbol_id(file_path: str, qualified_name: str, kind: str) -> str:
    """Computes a stable, deterministic, line-independent SHA256 identity for a symbol."""
    p = file_path.replace("\\", "/")
    if "/velune/" in p:
        p = p.split("/velune/", 1)[1]
        p = "velune/" + p
    elif p.startswith("c:") or p.startswith("C:") or ":" in p:
        p = p.split(":", 1)[1].lstrip("/")

    payload = f"{p}:{qualified_name}:{kind.lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RepositoryLanguage(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    GO = "go"
    RUST = "rust"
    JAVA = "java"
    # C and C++ share one bucket: headers are indistinguishable by extension
    # and the regex symbol patterns overlap.
    CPP = "cpp"
    CSHARP = "csharp"
    PHP = "php"
    RUBY = "ruby"
    SWIFT = "swift"
    KOTLIN = "kotlin"
    VUE = "vue"
    SVELTE = "svelte"
    HTML = "html"
    TEMPLATE = "template"  # Jinja/Jinja2 and similar templating languages
    SQL = "sql"
    GRAPHQL = "graphql"
    PRISMA = "prisma"
    PROTO = "proto"
    DART = "dart"
    OBJECTIVE_C = "objective_c"
    SCALA = "scala"
    CLOJURE = "clojure"
    ELIXIR = "elixir"
    ERLANG = "erlang"
    HASKELL = "haskell"
    OCAML = "ocaml"
    FSHARP = "fsharp"
    LUA = "lua"
    R = "r"
    JULIA = "julia"
    SHELL = "shell"
    POWERSHELL = "powershell"
    UNKNOWN = "unknown"


# Single source of truth for "which language does this extension belong to",
# shared by FilesystemScanner (discovery: which files to walk at all) and
# RepositorySnapshotParser (classification: what language a discovered file
# is). These used to be two independently-maintained tables that could
# silently disagree — an extension discovered by the scanner but absent from
# the parser's mapping fell through to RepositoryLanguage.UNKNOWN with no
# error, and conversely the parser recognized a few extensions (.cc/.cxx/
# .hpp/.hh) the scanner never discovered in the first place, making those
# parser branches dead code. Every extension the scanner walks is listed
# here with a real language tag; having a tag does not by itself imply a
# structural (AST/tree-sitter/regex) symbol extractor exists for it — see
# RepositorySnapshotParser for which languages have one.
EXTENSION_LANGUAGE_MAP: dict[str, RepositoryLanguage] = {
    # Core languages
    ".py": RepositoryLanguage.PYTHON,
    # Jupyter notebooks — the kernel is Python in the overwhelming majority
    # of cases and RepositorySnapshotParser reconstructs synthetic Python
    # source from the notebook's code cells before parsing; a non-Python
    # kernel degrades to zero symbols rather than a wrong parse (see
    # RepositorySnapshotParser._extract_notebook_source).
    ".ipynb": RepositoryLanguage.PYTHON,
    ".js": RepositoryLanguage.JAVASCRIPT,
    ".jsx": RepositoryLanguage.JAVASCRIPT,
    ".ts": RepositoryLanguage.TYPESCRIPT,
    ".tsx": RepositoryLanguage.TYPESCRIPT,
    ".go": RepositoryLanguage.GO,
    ".rs": RepositoryLanguage.RUST,
    ".java": RepositoryLanguage.JAVA,
    ".c": RepositoryLanguage.CPP,
    ".cpp": RepositoryLanguage.CPP,
    ".cc": RepositoryLanguage.CPP,
    ".cxx": RepositoryLanguage.CPP,
    ".h": RepositoryLanguage.CPP,
    ".hpp": RepositoryLanguage.CPP,
    ".hh": RepositoryLanguage.CPP,
    ".cs": RepositoryLanguage.CSHARP,
    ".php": RepositoryLanguage.PHP,
    ".rb": RepositoryLanguage.RUBY,
    ".swift": RepositoryLanguage.SWIFT,
    ".kt": RepositoryLanguage.KOTLIN,
    # Frontend frameworks
    ".vue": RepositoryLanguage.VUE,
    ".svelte": RepositoryLanguage.SVELTE,
    # Templates / markup
    ".html": RepositoryLanguage.HTML,
    ".htm": RepositoryLanguage.HTML,
    ".jinja": RepositoryLanguage.TEMPLATE,
    ".jinja2": RepositoryLanguage.TEMPLATE,
    ".j2": RepositoryLanguage.TEMPLATE,
    # Query languages
    ".sql": RepositoryLanguage.SQL,
    ".graphql": RepositoryLanguage.GRAPHQL,
    ".gql": RepositoryLanguage.GRAPHQL,
    # Schema / config files that are code
    ".prisma": RepositoryLanguage.PRISMA,
    ".proto": RepositoryLanguage.PROTO,
    # Mobile
    ".dart": RepositoryLanguage.DART,
    ".m": RepositoryLanguage.OBJECTIVE_C,
    ".mm": RepositoryLanguage.OBJECTIVE_C,
    # Other compiled/functional languages
    ".scala": RepositoryLanguage.SCALA,
    ".clj": RepositoryLanguage.CLOJURE,
    ".cljs": RepositoryLanguage.CLOJURE,
    ".ex": RepositoryLanguage.ELIXIR,
    ".exs": RepositoryLanguage.ELIXIR,
    ".erl": RepositoryLanguage.ERLANG,
    ".hs": RepositoryLanguage.HASKELL,
    ".ml": RepositoryLanguage.OCAML,
    ".fs": RepositoryLanguage.FSHARP,
    ".fsi": RepositoryLanguage.FSHARP,
    ".fsx": RepositoryLanguage.FSHARP,
    ".lua": RepositoryLanguage.LUA,
    ".r": RepositoryLanguage.R,
    ".R": RepositoryLanguage.R,
    ".jl": RepositoryLanguage.JULIA,
    # Shell scripts (often contain important wiring logic)
    ".sh": RepositoryLanguage.SHELL,
    ".bash": RepositoryLanguage.SHELL,
    ".zsh": RepositoryLanguage.SHELL,
    ".fish": RepositoryLanguage.SHELL,
    ".ps1": RepositoryLanguage.POWERSHELL,
}


class RepositorySymbolKind(StrEnum):
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    IMPORT = "import"
    UNKNOWN = "unknown"


class RepositorySymbol(BaseModel):
    name: str
    kind: RepositorySymbolKind
    file_path: str
    line_start: int = 1
    line_end: int = 1
    docstring: str | None = None
    parent: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    symbol_id: str | None = None
    qualified_name: str | None = None

    @model_validator(mode="after")
    def populate_identity(self) -> "RepositorySymbol":
        """Ensures stable symbol_id and qualified_name are automatically calculated if missing."""
        if not self.qualified_name:
            self.qualified_name = build_qualified_name(self.file_path, self.name, self.parent)
        if not self.symbol_id:
            self.symbol_id = compute_symbol_id(self.file_path, self.qualified_name, self.kind.value)
        return self


# Files larger than this are recorded (path, size, language, content hash)
# but never fully read for structural (AST/tree-sitter/regex) parsing. Without
# this, one oversized vendored/generated file sitting in a directory name the
# scanner doesn't special-case (not node_modules/dist/etc.) dominates parse
# time and symbol/edge counts in proportion to its own size, not the size of
# the real payload — confirmed in practice (baseline §6.4: a single 800KB/
# 60,000-line vendored file produced 20,013 of 20,015 total symbols and
# pushed a ~30ms index to 4.3s). Such a file is still discovered, hashed,
# and counted toward file/language stats — it is opaque to symbol extraction,
# not invisible. See RepositoryFile.metadata["opaque"].
MAX_STRUCTURAL_PARSE_BYTES = 512 * 1024  # 512 KB

# Markers conventionally placed near the top of machine-generated files
# (protobuf/gRPC stubs, OpenAPI clients, ORM migrations, codegen output).
# Checked against only the first GENERATED_MARKER_SCAN_CHARS of a file's
# content — cheap since that content is already being read to parse the file
# anyway, unlike the size guard above which exists specifically to avoid a
# read. Matched case-sensitively: these are conventional exact phrasings, not
# prose, so case-insensitive matching would trade precision for no real gain.
GENERATED_FILE_MARKERS: tuple[str, ...] = (
    "@generated",
    "DO NOT EDIT",
    "DO NOT MODIFY",
    "Code generated by",
    "This file was automatically generated",
    "This is an auto-generated file",
    "Autogenerated by",
    "AUTO-GENERATED FILE",
)
GENERATED_MARKER_SCAN_CHARS = 500


def is_generated_content(text: str) -> bool:
    """True if *text* (a file's leading content) carries a generated-file marker.

    A generated file still deserves the opaque-file treatment the size guard
    gives oversized files — recorded and language-tagged, but not
    structurally parsed — since its symbols are noise for a human/agent
    reasoning about the hand-authored parts of the codebase, and it can be
    regenerated wholesale rather than edited, unlike ordinary source.
    """
    excerpt = text[:GENERATED_MARKER_SCAN_CHARS]
    return any(marker in excerpt for marker in GENERATED_FILE_MARKERS)


class RepositoryFile(BaseModel):
    path: str
    language: RepositoryLanguage
    size_bytes: int
    sha256: str
    symbols: list[RepositorySymbol] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepositoryEdge(BaseModel):
    source: str
    target: str
    edge_type: str  # e.g., "imports", "calls", "contains"
    weight: float = 1.0


class RepositorySnapshot(BaseModel):
    root_path: str
    files: list[RepositoryFile] = Field(default_factory=list)
    symbols: list[RepositorySymbol] = Field(default_factory=list)
    edges: list[RepositoryEdge] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)

    # The API connection map (routes, frontend calls, DB queries) built by
    # repository/api_mapper.py. Declared here because it must be: this is a
    # Pydantic v2 model, so the previous `snapshot.api_map = api_map` assignment
    # on an undeclared attribute raised ValueError on *every* index run. The
    # raise was swallowed by a broad `except Exception` upstream, so the whole
    # API-map feature was silently dead.
    #
    # Typed `Any` rather than `APIConnectionMap` on purpose: that type lives in
    # api_mapper.py, a heavy regex-laden module, and schemas.py is imported
    # nearly everywhere. The map is only ever read back via attribute access,
    # never validated or serialised, so the import cost buys nothing.
    api_map: Any = None
