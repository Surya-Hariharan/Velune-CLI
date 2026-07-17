"""Cross-stack API connection mapper.

Extracts HTTP routes (backend), API calls (frontend), and database queries, then
stitches them into an APIConnectionMap that the context builder surfaces to the LLM.

This is the missing layer that lets Velune reason safely about changes that span
frontend → backend → database without destroying the calling chain.

Supported backends:
  FastAPI / Starlette  — @router.get/post/put/delete/patch("/path")
  Flask                — @app.route("/path", methods=[...])  |  @bp.route(...)
  Express.js / Koa     — router.get('/path', ...)  |  app.post('/path', ...)
  Django               — path('api/...', view)  in urls.py
  Next.js App Router   — file-based: app/api/**/route.ts  (GET/POST exports)
  Next.js Pages Router — file-based: pages/api/**/*.ts

Supported frontend callers:
  Fetch API            — fetch('/api/...')  |  fetch(`/api/${id}`)
  Axios                — axios.get('/api/...')  |  axios.post(...)
  React Query          — useQuery(['k', '/api/...'])  |  useMutation(...)
  SWR                  — useSWR('/api/...')

Supported DB layers:
  Prisma               — prisma.<model>.<method>(
  SQLAlchemy           — session.query(Model)  |  db.session.query(...)
  Mongoose             — Model.find(  |  Model.findById(  |  Model.findOne(
  Django ORM           — Model.objects.all()  |  .filter(  |  .create(
  Raw SQL              — SELECT/INSERT/UPDATE/DELETE in string literals
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("velune.repository.api_mapper")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RouteEndpoint:
    """A single HTTP route declared in backend code."""

    method: str  # GET POST PUT DELETE PATCH * (wildcard)
    path: str  # normalised URL path, e.g. /api/users/{id}
    file: str  # workspace-relative file path
    line: int  # 1-based line where route is declared
    handler: str  # function/handler name if detectable, else ""
    framework: str  # "fastapi" | "flask" | "express" | "django" | "nextjs"


@dataclass
class FrontendCall:
    """A single HTTP call made from frontend code."""

    method: str  # GET POST PUT DELETE PATCH UNKNOWN
    url: str  # raw URL string extracted from source
    file: str
    line: int
    caller: str  # wrapping function/component name if detectable, else ""


@dataclass
class DBQuery:
    """A single database query/operation."""

    operation: str  # SELECT INSERT UPDATE DELETE FIND CREATE UPSERT etc.
    model: str  # table/model name if detectable, else ""
    file: str
    line: int
    orm: str  # "prisma" | "sqlalchemy" | "mongoose" | "django" | "raw_sql"


@dataclass
class RouteConnection:
    """Resolved connection between a backend route and its callers + DB ops."""

    route: RouteEndpoint
    frontend_callers: list[FrontendCall] = field(default_factory=list)
    db_queries: list[DBQuery] = field(default_factory=list)


@dataclass
class APIConnectionMap:
    """Full cross-stack connection map for the workspace."""

    routes: list[RouteEndpoint] = field(default_factory=list)
    frontend_calls: list[FrontendCall] = field(default_factory=list)
    db_queries: list[DBQuery] = field(default_factory=list)
    connections: list[RouteConnection] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.routes and not self.frontend_calls and not self.db_queries

    def to_dict(self) -> dict:
        """Serialize for the pipeline cache (``connections`` is re-derived on load, not stored)."""
        return {
            "routes": [r.__dict__ for r in self.routes],
            "frontend_calls": [c.__dict__ for c in self.frontend_calls],
            "db_queries": [q.__dict__ for q in self.db_queries],
        }

    @classmethod
    def from_dict(cls, data: dict) -> APIConnectionMap:
        """Deserialize routes/calls/queries and re-derive ``connections``."""
        amap = cls(
            routes=[RouteEndpoint(**r) for r in data.get("routes", [])],
            frontend_calls=[FrontendCall(**c) for c in data.get("frontend_calls", [])],
            db_queries=[DBQuery(**q) for q in data.get("db_queries", [])],
        )
        amap.connections = resolve_connections(amap)
        return amap


def resolve_connections(amap: APIConnectionMap) -> list[RouteConnection]:
    """Match frontend calls to backend routes, attach relevant DB queries.

    Module-level (not a method) so :meth:`APIConnectionMap.from_dict` can
    re-derive ``connections`` after deserializing a cached map without
    needing an ``APIMapper`` instance.
    """
    connections: list[RouteConnection] = []

    for route in amap.routes:
        conn = RouteConnection(route=route)

        # Match frontend callers
        for call in amap.frontend_calls:
            if _url_matches_route(call.url, route.path):
                # Filter by method where possible
                if (
                    route.method in ("*", call.method)
                    or call.method == "GET"
                    and route.method == "GET"
                ):
                    conn.frontend_callers.append(call)

        # Attach DB queries from the same file as the route
        for q in amap.db_queries:
            if q.file == route.file:
                # Only include queries within ~50 lines of the handler declaration
                if abs(q.line - route.line) <= 80:
                    conn.db_queries.append(q)

        connections.append(conn)

    # Also emit orphan frontend calls (no matched route) as bare connections with no route
    matched_calls = {id(c) for conn in connections for c in conn.frontend_callers}
    for call in amap.frontend_calls:
        if id(call) not in matched_calls:
            # Synthetic route placeholder for unmatched calls
            synthetic = RouteEndpoint(
                method=call.method,
                path=call.url,
                file="(unknown)",
                line=0,
                handler="",
                framework="unknown",
            )
            connections.append(RouteConnection(route=synthetic, frontend_callers=[call]))

    return connections


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_HTTP_METHODS = r"(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)"

# FastAPI / Starlette
_FASTAPI_ROUTE = re.compile(
    r"@(?:\w+\.)?(?:get|post|put|delete|patch|head|options)\s*\(\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_FASTAPI_METHOD = re.compile(
    r"@(?:\w+\.)?(?P<method>get|post|put|delete|patch|head|options)\s*\(",
    re.IGNORECASE,
)

# Flask
_FLASK_ROUTE = re.compile(
    r'@\w+\.route\s*\(\s*[\'"]([^\'"]+)[\'"](?:[^)]*methods\s*=\s*\[([^\]]+)\])?',
    re.IGNORECASE,
)

# Express.js / Koa
_EXPRESS_ROUTE = re.compile(
    r'(?:router|app|server)\s*\.\s*(?P<method>get|post|put|delete|patch|all|use)\s*\(\s*[\'"`]([^\'"`]+)[\'"`]',
    re.IGNORECASE,
)

# Django urls.py
_DJANGO_PATH = re.compile(
    r'(?:path|re_path|url)\s*\(\s*[\'"]([^\'"]+)[\'"]',
    re.IGNORECASE,
)

# Next.js App Router exports
_NEXTJS_EXPORT = re.compile(
    r"export\s+(?:async\s+)?(?:function|const)\s+(?P<method>GET|POST|PUT|DELETE|PATCH)\s*[=(]",
)

# Handler / function name on the next line after a decorator
_PY_FUNC_DEF = re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\(")
_JS_FUNC_NAME = re.compile(
    r"(?:function\s+(\w+)|const\s+(\w+)\s*=\s*(?:async\s*)?\(|(\w+)\s*:\s*(?:async\s*)?\()"
)

# ---------------------------------------------------------------------------
# Frontend call patterns
# ---------------------------------------------------------------------------

_FETCH_CALL = re.compile(
    r"""fetch\s*\(\s*(?P<quote>['"` ])(?P<url>[^'"` ]+)(?P=quote)""",
    re.IGNORECASE,
)
_FETCH_TEMPLATE = re.compile(
    r"fetch\s*\(\s*`(?P<url>[^`]+)`",
    re.IGNORECASE,
)
_AXIOS_CALL = re.compile(
    r"""axios\s*\.\s*(?P<method>get|post|put|delete|patch|head)\s*\(\s*(?P<quote>['"` ])(?P<url>[^'"` ]+)(?P=quote)""",
    re.IGNORECASE,
)
_AXIOS_TEMPLATE = re.compile(
    r"axios\s*\.\s*(?P<method>get|post|put|delete|patch|head)\s*\(\s*`(?P<url>[^`]+)`",
    re.IGNORECASE,
)
_SWR_CALL = re.compile(
    r"""useSWR\s*\(\s*(?P<quote>['"` ])(?P<url>[^'"` ]+)(?P=quote)""",
    re.IGNORECASE,
)
_RQUERY_CALL = re.compile(
    r"""useQuery\s*\(\s*\[([^\]]+)\]""",
    re.IGNORECASE,
)

# Wrapping function/component name
_JS_COMPONENT_FUNC = re.compile(
    r"(?:function|const|let|var)\s+(\w+)\s*[=(]|export\s+default\s+function\s+(\w+)",
)

# ---------------------------------------------------------------------------
# Database patterns
# ---------------------------------------------------------------------------

_PRISMA_CALL = re.compile(
    r"prisma\s*\.\s*(?P<model>\w+)\s*\.\s*(?P<op>findMany|findUnique|findFirst|create|update|upsert|delete|deleteMany|count|aggregate|groupBy|createMany|updateMany)\s*\(",
    re.IGNORECASE,
)
_SQLALCHEMY_CALL = re.compile(
    r"(?:session|db\.session|db)\s*\.\s*(?:query\s*\(\s*(?P<model>\w+)|(?P<op>add|delete|commit|execute)\s*\()",
    re.IGNORECASE,
)
_SQLALCHEMY_SELECT = re.compile(
    r"(?:select|Select)\s*\(\s*(?P<model>\w+)\s*\)",
)
_MONGOOSE_CALL = re.compile(
    r"(?P<model>\w+)\s*\.\s*(?P<op>find|findById|findOne|findByIdAndUpdate|findOneAndUpdate|findByIdAndDelete|create|insertMany|updateMany|updateOne|deleteMany|deleteOne|countDocuments|aggregate)\s*\(",
)
_DJANGO_ORM = re.compile(
    r"(?P<model>\w+)\s*\.\s*objects\s*\.\s*(?P<op>all|filter|get|create|update|delete|bulk_create|bulk_update|first|last|count|aggregate|annotate|values|exclude)\s*\(",
)
_RAW_SQL = re.compile(
    r"(?P<op>SELECT|INSERT|UPDATE|DELETE)\s+(?:INTO\s+)?(?:FROM\s+)?(?P<model>\w+)?",
    re.IGNORECASE,
)

# URL path normalisation
_PATH_PARAM_COLON = re.compile(r":(\w+)")  # Express :param → {param}
_PATH_PARAM_ANGLE = re.compile(r"<(?:\w+:)?(\w+)>")  # Flask <type:name> → {name}


def _normalise_path(path: str) -> str:
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    p = _PATH_PARAM_COLON.sub(r"{\1}", p)
    p = _PATH_PARAM_ANGLE.sub(r"{\1}", p)
    return p.rstrip("/") or "/"


def _url_matches_route(url: str, route_path: str) -> bool:
    """Fuzzy match: does a frontend URL string likely target this route path?"""
    # strip template literal expressions: /api/users/${id} → /api/users/
    url_clean = re.sub(r"\$\{[^}]+\}", "", url).rstrip("/") or "/"
    # strip path params: /api/users/{id} → /api/users/
    route_clean = re.sub(r"\{[^}]+\}", "", route_path).rstrip("/") or "/"
    return url_clean == route_clean or url_clean.startswith(route_clean)


# ---------------------------------------------------------------------------
# Main mapper
# ---------------------------------------------------------------------------


class APIMapper:
    """Scans workspace files and produces an APIConnectionMap."""

    # Extensions we actually parse
    _BACKEND_EXTS = {".py", ".ts", ".js", ".tsx", ".jsx"}
    _FRONTEND_EXTS = {".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte"}
    # Skip these directories
    _SKIP_DIRS = {
        "node_modules",
        ".git",
        ".velune",
        "__pycache__",
        ".mypy_cache",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "coverage",
        "venv",
        ".venv",
        "env",
        ".env",
        "site-packages",
    }
    # Max file size to parse (bytes)
    _MAX_FILE_BYTES = 512_000

    def __init__(self, root_path: Path) -> None:
        self.root_path = root_path.resolve()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_map(self, file_paths: list[str]) -> APIConnectionMap:
        """Scan the given file list and return the full APIConnectionMap."""
        amap = APIConnectionMap()

        for rel_path in file_paths:
            routes, calls, queries = self.scan_file(rel_path)
            amap.routes.extend(routes)
            amap.db_queries.extend(queries)
            amap.frontend_calls.extend(calls)

        amap.connections = self._resolve_connections(amap)
        return amap

    def scan_file(
        self, rel_path: str
    ) -> tuple[list[RouteEndpoint], list[FrontendCall], list[DBQuery]]:
        """Extract routes, frontend calls, and DB queries from a single file.

        Split out of :meth:`build_map` so callers with a file-level delta
        (added/updated files only) can re-scan just those files instead of
        the whole workspace — see :meth:`build_map_incremental`.
        """
        abs_path = self.root_path / rel_path
        try:
            content, lines = self._read_file(abs_path)
        except Exception:
            return [], [], []
        if content is None:
            return [], [], []

        ext = Path(rel_path).suffix.lower()
        norm = rel_path.replace("\\", "/")

        routes: list[RouteEndpoint] = []
        queries: list[DBQuery] = []
        calls: list[FrontendCall] = []

        if ext in self._BACKEND_EXTS:
            routes = self._extract_routes(norm, content, lines)
            queries = self._extract_db_queries(norm, content, lines)

        if ext in self._FRONTEND_EXTS:
            calls = self._extract_frontend_calls(norm, content, lines)

        return routes, calls, queries

    def build_map_incremental(
        self,
        previous: APIConnectionMap | None,
        changed_files: list[str],
        removed_files: list[str],
    ) -> APIConnectionMap:
        """Reuse *previous*'s entries for untouched files; re-scan only the delta.

        ``connections`` still gets re-derived over the merged (kept + rescanned)
        lists — that step is pure in-memory matching (no I/O), so it's cheap
        even though it isn't itself scoped to the delta.

        Falls back to a full :meth:`build_map` over ``changed_files`` when
        there is no previous map to diff against (cold start).
        """
        if previous is None:
            return self.build_map(changed_files)

        stale = set(changed_files) | set(removed_files)
        routes = [r for r in previous.routes if r.file not in stale]
        frontend_calls = [c for c in previous.frontend_calls if c.file not in stale]
        db_queries = [q for q in previous.db_queries if q.file not in stale]

        for rel_path in changed_files:
            new_routes, new_calls, new_queries = self.scan_file(rel_path)
            routes.extend(new_routes)
            frontend_calls.extend(new_calls)
            db_queries.extend(new_queries)

        amap = APIConnectionMap(routes=routes, frontend_calls=frontend_calls, db_queries=db_queries)
        amap.connections = self._resolve_connections(amap)
        return amap

    # ------------------------------------------------------------------
    # Route extraction
    # ------------------------------------------------------------------

    def _extract_routes(
        self, file_path: str, content: str, lines: list[str]
    ) -> list[RouteEndpoint]:
        routes: list[RouteEndpoint] = []
        ext = Path(file_path).suffix.lower()

        if ext == ".py":
            routes.extend(self._extract_python_routes(file_path, lines))
        elif ext in {".ts", ".tsx", ".js", ".jsx"}:
            routes.extend(self._extract_js_routes(file_path, lines))

        return routes

    def _extract_python_routes(self, file_path: str, lines: list[str]) -> list[RouteEndpoint]:
        routes: list[RouteEndpoint] = []

        for i, line in enumerate(lines, 1):
            stripped = line.strip()

            # FastAPI / Starlette
            m = _FASTAPI_ROUTE.search(stripped)
            if m:
                method_m = _FASTAPI_METHOD.search(stripped)
                method = method_m.group("method").upper() if method_m else "GET"
                path = _normalise_path(m.group(1))
                handler = self._next_python_func(lines, i)
                routes.append(
                    RouteEndpoint(
                        method=method,
                        path=path,
                        file=file_path,
                        line=i,
                        handler=handler,
                        framework="fastapi",
                    )
                )
                continue

            # Flask
            m = _FLASK_ROUTE.search(stripped)
            if m:
                path = _normalise_path(m.group(1))
                methods_raw = m.group(2) or ""
                if methods_raw:
                    for meth in re.findall(r"'([A-Z]+)'|\"([A-Z]+)\"", methods_raw):
                        method = (meth[0] or meth[1]).upper()
                        handler = self._next_python_func(lines, i)
                        routes.append(
                            RouteEndpoint(
                                method=method,
                                path=path,
                                file=file_path,
                                line=i,
                                handler=handler,
                                framework="flask",
                            )
                        )
                else:
                    handler = self._next_python_func(lines, i)
                    routes.append(
                        RouteEndpoint(
                            method="GET",
                            path=path,
                            file=file_path,
                            line=i,
                            handler=handler,
                            framework="flask",
                        )
                    )
                continue

            # Django urls.py
            if "urls" in file_path.lower() and _DJANGO_PATH.search(stripped):
                m2 = _DJANGO_PATH.search(stripped)
                if m2:
                    path = _normalise_path(m2.group(1))
                    routes.append(
                        RouteEndpoint(
                            method="*",
                            path=path,
                            file=file_path,
                            line=i,
                            handler="",
                            framework="django",
                        )
                    )

        return routes

    def _extract_js_routes(self, file_path: str, lines: list[str]) -> list[RouteEndpoint]:
        routes: list[RouteEndpoint] = []
        norm = file_path.replace("\\", "/")

        # Next.js App Router — file path encodes the route
        if (
            "/app/" in norm
            and norm.endswith("/route.ts")
            or norm.endswith("/route.tsx")
            or norm.endswith("/route.js")
        ):
            derived_path = self._nextjs_app_path(norm)
            for i, line in enumerate(lines, 1):
                m = _NEXTJS_EXPORT.search(line)
                if m:
                    method = m.group("method").upper()
                    routes.append(
                        RouteEndpoint(
                            method=method,
                            path=derived_path,
                            file=file_path,
                            line=i,
                            handler=method,
                            framework="nextjs",
                        )
                    )
            return routes

        # Next.js Pages Router
        if "/pages/api/" in norm:
            derived_path = self._nextjs_pages_path(norm)
            # All exports are handlers for this endpoint
            routes.append(
                RouteEndpoint(
                    method="*",
                    path=derived_path,
                    file=file_path,
                    line=1,
                    handler="",
                    framework="nextjs",
                )
            )
            return routes

        # Express / Koa / generic
        for i, line in enumerate(lines, 1):
            m = _EXPRESS_ROUTE.search(line)
            if m:
                method = m.group("method").upper()
                if method == "USE":
                    method = "*"
                path = _normalise_path(m.group(2))
                routes.append(
                    RouteEndpoint(
                        method=method,
                        path=path,
                        file=file_path,
                        line=i,
                        handler="",
                        framework="express",
                    )
                )

        return routes

    def _nextjs_app_path(self, norm_path: str) -> str:
        # Extract between /app/ and /route.ts
        m = re.search(r"/app/(.+?)/route\.[jt]sx?", norm_path)
        if m:
            seg = m.group(1)
            # Remove (group) segments used for layout
            seg = re.sub(r"\([^)]+\)/", "", seg)
            # Dynamic [param] → {param}
            seg = re.sub(r"\[([^\]]+)\]", r"{\1}", seg)
            return "/" + seg
        return "/api/unknown"

    def _nextjs_pages_path(self, norm_path: str) -> str:
        m = re.search(r"/pages/api/(.+?)\.[jt]sx?$", norm_path)
        if m:
            seg = m.group(1)
            seg = re.sub(r"\[([^\]]+)\]", r"{\1}", seg)
            if seg == "index":
                return "/api"
            return "/api/" + seg
        return "/api/unknown"

    # ------------------------------------------------------------------
    # Frontend call extraction
    # ------------------------------------------------------------------

    def _extract_frontend_calls(
        self, file_path: str, content: str, lines: list[str]
    ) -> list[FrontendCall]:
        calls: list[FrontendCall] = []
        current_component = ""

        for i, line in enumerate(lines, 1):
            # Track current function/component scope
            comp_m = _JS_COMPONENT_FUNC.search(line)
            if comp_m:
                current_component = comp_m.group(1) or comp_m.group(2) or current_component

            # fetch()
            for m in _FETCH_CALL.finditer(line):
                url = m.group("url")
                if url.startswith("/") or url.startswith("http"):
                    calls.append(
                        FrontendCall(
                            method="GET",
                            url=url,
                            file=file_path,
                            line=i,
                            caller=current_component,
                        )
                    )

            for m in _FETCH_TEMPLATE.finditer(line):
                url = m.group("url")
                if "/" in url:
                    calls.append(
                        FrontendCall(
                            method="GET",
                            url=url,
                            file=file_path,
                            line=i,
                            caller=current_component,
                        )
                    )

            # axios.method()
            for m in _AXIOS_CALL.finditer(line):
                url = m.group("url")
                if url.startswith("/") or url.startswith("http"):
                    calls.append(
                        FrontendCall(
                            method=m.group("method").upper(),
                            url=url,
                            file=file_path,
                            line=i,
                            caller=current_component,
                        )
                    )

            for m in _AXIOS_TEMPLATE.finditer(line):
                url = m.group("url")
                if "/" in url:
                    calls.append(
                        FrontendCall(
                            method=m.group("method").upper(),
                            url=url,
                            file=file_path,
                            line=i,
                            caller=current_component,
                        )
                    )

            # useSWR
            for m in _SWR_CALL.finditer(line):
                url = m.group("url")
                if url.startswith("/") or url.startswith("http"):
                    calls.append(
                        FrontendCall(
                            method="GET",
                            url=url,
                            file=file_path,
                            line=i,
                            caller=current_component,
                        )
                    )

            # useQuery(['key', '/api/...'])
            for m in _RQUERY_CALL.finditer(line):
                inner = m.group(1)
                url_m = re.search(r"['\"`]([/][^'\"` ]+)['\"`]", inner)
                if url_m:
                    calls.append(
                        FrontendCall(
                            method="GET",
                            url=url_m.group(1),
                            file=file_path,
                            line=i,
                            caller=current_component,
                        )
                    )

        return calls

    # ------------------------------------------------------------------
    # DB query extraction
    # ------------------------------------------------------------------

    def _extract_db_queries(self, file_path: str, content: str, lines: list[str]) -> list[DBQuery]:
        queries: list[DBQuery] = []

        for i, line in enumerate(lines, 1):
            # Prisma
            m = _PRISMA_CALL.search(line)
            if m:
                op = m.group("op").upper()
                model = m.group("model")
                queries.append(
                    DBQuery(
                        operation=op,
                        model=model,
                        file=file_path,
                        line=i,
                        orm="prisma",
                    )
                )
                continue

            # SQLAlchemy
            m = _SQLALCHEMY_SELECT.search(line)
            if m:
                queries.append(
                    DBQuery(
                        operation="SELECT",
                        model=m.group("model"),
                        file=file_path,
                        line=i,
                        orm="sqlalchemy",
                    )
                )
                continue
            m = _SQLALCHEMY_CALL.search(line)
            if m:
                model = m.group("model") or ""
                op = m.group("op") or "QUERY"
                queries.append(
                    DBQuery(
                        operation=op.upper(),
                        model=model,
                        file=file_path,
                        line=i,
                        orm="sqlalchemy",
                    )
                )
                continue

            # Django ORM
            m = _DJANGO_ORM.search(line)
            if m:
                queries.append(
                    DBQuery(
                        operation=m.group("op").upper(),
                        model=m.group("model"),
                        file=file_path,
                        line=i,
                        orm="django",
                    )
                )
                continue

            # Mongoose (only match on likely model names — PascalCase)
            m = _MONGOOSE_CALL.search(line)
            if m and m.group("model")[0].isupper():
                queries.append(
                    DBQuery(
                        operation=m.group("op").upper(),
                        model=m.group("model"),
                        file=file_path,
                        line=i,
                        orm="mongoose",
                    )
                )
                continue

            # Raw SQL (only inside string literals to reduce noise)
            if re.search(r"""['"`].*(?:SELECT|INSERT|UPDATE|DELETE).*['"`]""", line, re.IGNORECASE):
                m = _RAW_SQL.search(line)
                if m:
                    queries.append(
                        DBQuery(
                            operation=m.group("op").upper(),
                            model=m.group("model") or "",
                            file=file_path,
                            line=i,
                            orm="raw_sql",
                        )
                    )

        return queries

    # ------------------------------------------------------------------
    # Connection resolution
    # ------------------------------------------------------------------

    def _resolve_connections(self, amap: APIConnectionMap) -> list[RouteConnection]:
        """Match frontend calls to backend routes, attach relevant DB queries."""
        return resolve_connections(amap)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_file(self, path: Path) -> tuple[str | None, list[str]]:
        if not path.exists() or not path.is_file():
            return None, []
        if path.stat().st_size > self._MAX_FILE_BYTES:
            return None, []
        content = path.read_text(encoding="utf-8", errors="ignore")
        return content, content.splitlines()

    @staticmethod
    def _next_python_func(lines: list[str], after_line: int) -> str:
        """Return the function name defined within 3 lines after *after_line*."""
        for j in range(after_line, min(after_line + 3, len(lines))):
            m = _PY_FUNC_DEF.match(lines[j].strip())
            if m:
                return m.group(1)
        return ""


# ---------------------------------------------------------------------------
# Context text renderer
# ---------------------------------------------------------------------------

_MAX_CONNECTIONS_IN_CONTEXT = 30
_MAX_CALLERS_PER_ROUTE = 4
_MAX_DB_PER_ROUTE = 4


def render_api_map(amap: APIConnectionMap, max_tokens: int = 1500) -> str | None:
    """Render APIConnectionMap to a compact text block for LLM context.

    Returns None when the map is empty (no routes and no calls).
    """
    if amap.is_empty:
        return None

    lines: list[str] = []
    budget = max_tokens * 4  # ~4 chars/token

    # Summary header
    n_routes = len(amap.routes)
    n_calls = len(amap.frontend_calls)
    n_queries = len(amap.db_queries)
    header = (
        f"[API CONNECTION MAP  routes:{n_routes}  frontend-calls:{n_calls}  db-ops:{n_queries}]"
    )
    lines.append(header)
    budget -= len(header)

    # Emit connections with matched routes first, then orphan calls
    matched_connections = [c for c in amap.connections if c.route.framework != "unknown"]
    orphan_connections = [c for c in amap.connections if c.route.framework == "unknown"]

    rendered = 0
    for conn in matched_connections[:_MAX_CONNECTIONS_IN_CONTEXT]:
        if budget < 80:
            lines.append("  ... (truncated — too many routes)")
            break
        block = _render_connection(conn)
        lines.append(block)
        budget -= len(block)
        rendered += 1

    # Orphan calls (frontend → unknown backend)
    if orphan_connections and budget > 200:
        lines.append("[UNMATCHED FRONTEND CALLS — no backend route found]")
        for conn in orphan_connections[:10]:
            call = conn.frontend_callers[0]
            entry = f"  {call.method} {call.url}  ← {call.file}:{call.line}"
            lines.append(entry)
            budget -= len(entry)
            if budget < 80:
                break

    return "\n".join(lines)


def _render_connection(conn: RouteConnection) -> str:
    route = conn.route
    method_str = route.method if route.method != "*" else "ANY"
    handler_str = f"  handler: {route.handler}" if route.handler else ""
    block_lines = [
        f"\n{method_str} {route.path}",
        f"  → {route.file}:{route.line}{handler_str}",
    ]

    if conn.db_queries:
        for q in conn.db_queries[:_MAX_DB_PER_ROUTE]:
            model_str = f" ({q.model})" if q.model else ""
            block_lines.append(f"  db: {q.orm}.{q.operation}{model_str}  line {q.line}")

    if conn.frontend_callers:
        for c in conn.frontend_callers[:_MAX_CALLERS_PER_ROUTE]:
            caller_str = f" [{c.caller}]" if c.caller else ""
            block_lines.append(f"  ← {c.file}:{c.line}{caller_str}")

    return "\n".join(block_lines)
