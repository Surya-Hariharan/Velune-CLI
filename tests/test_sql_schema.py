"""Schema discovery: bounded, structured, paged, privilege-filtered, injection-proof (VAIR A5)."""

from __future__ import annotations

import pytest

from velune.resources.base import ErrorCode


@pytest.fixture
def pg(sql_env):
    sql_env.add("dev")
    return sql_env.pg()


@pytest.fixture
def my(sql_env):
    sql_env.add("dev", engine="mysql")
    return sql_env.mysql()


def last_sql(env):
    return env.db.executed[-1]


# ── Database metadata, schemas ──────────────────────────────────────────────


async def test_database_info(pg, sql_env):
    sql_env.db.on(
        "current_database()",
        ["database", "role", "version", "encoding", "timezone", "default_read_only"],
        [["app", "dev", "PostgreSQL 16.2", "UTF8", "UTC", "off"]],
    )
    result = await pg.execute("database_info", {})
    assert result.ok and result.data["database"] == "app" and result.data["role"] == "dev"
    assert result.data["connection"] == "dev" and "16.2" in result.data["version"]


async def test_list_databases_and_schemas_only_ask_for_what_the_role_can_use(pg, sql_env):
    sql_env.db.on("FROM pg_database", ["datname"], [["app"], ["analytics"]])
    sql_env.db.on("FROM pg_namespace", ["schema_name"], [["public"], ["billing"]])
    dbs = await pg.execute("list_databases", {})
    schemas = await pg.execute("list_schemas", {})
    assert dbs.data["rows"] == [["app"], ["analytics"]] and schemas.ok
    texts = " ".join(sql_env.db.user_sql())
    assert "has_database_privilege" in texts and "has_schema_privilege" in texts


# ── Tables and views: paged, filtered, bound ────────────────────────────────


async def test_list_tables_is_paged(pg, sql_env):
    rows = [["public", f"t{i:02d}", "table"] for i in range(51)]  # one more than the page
    sql_env.db.on("FROM information_schema.tables", ["schema", "name", "kind"], rows)
    page = await pg.execute("list_tables", {"limit": 50})
    assert page.ok and page.data["count"] == 50 and page.data["truncated"] is True
    assert page.data["next_offset"] == 50 and page.data["tables"][0] == {
        "schema": "public",
        "name": "t00",
        "kind": "table",
    }
    sql, params = last_sql(sql_env)
    assert sql.rstrip().endswith("LIMIT %s OFFSET %s") and params[-2:] == (51, 0)
    second = await pg.execute("list_tables", {"limit": 50, "offset": 50})
    assert last_sql(sql_env)[1][-2:] == (51, 50) and second.ok


async def test_a_small_listing_is_not_truncated(pg, sql_env):
    sql_env.db.on(
        "FROM information_schema.tables", ["schema", "name", "kind"], [["public", "users", "table"]]
    )
    out = await pg.execute("list_tables", {})
    assert out.data["truncated"] is False and "next_offset" not in out.data


async def test_filters_travel_as_bound_parameters_never_in_the_text(pg, sql_env):
    await pg.execute("list_tables", {"schema": "public", "name_like": "x'; DROP TABLE t; --"})
    sql, params = last_sql(sql_env)
    assert "DROP" not in sql and "public" not in sql
    assert "public" in params and any("DROP TABLE" in str(p) for p in params)
    assert "ILIKE %s ESCAPE '!'" in sql


async def test_like_wildcards_in_a_filter_are_escaped(pg, sql_env):
    await pg.execute("list_tables", {"name_like": "50%_off"})
    assert last_sql(sql_env)[1][-3] == "%50!%!_off%"


async def test_views_are_listed_separately(pg, sql_env):
    sql_env.db.on(
        "FROM information_schema.tables",
        ["schema", "name", "kind"],
        [["public", "v_users", "view"]],
    )
    out = await pg.execute("list_views", {})
    assert out.ok and out.data["views"][0]["name"] == "v_users"
    assert last_sql(sql_env)[1][0] == "VIEW"
    await pg.execute("list_tables", {})
    assert last_sql(sql_env)[1][0] == "BASE TABLE"


@pytest.mark.parametrize("action", ["list_tables", "list_views"])
@pytest.mark.parametrize("bad", ["bad;schema", "a b", "x'--", "é", "a" * 64, "..", "x`y"])
async def test_a_bad_schema_name_is_refused_before_any_query(pg, sql_env, action, bad):
    result = await pg.execute(action, {"schema": bad})
    assert (
        not result.ok and result.code is ErrorCode.VALIDATION_FAILED and sql_env.db.executed == []
    )


async def test_a_bad_table_name_is_refused(pg, sql_env):
    for action in ("describe_table", "list_indexes", "list_constraints", "table_relationships"):
        result = await pg.execute(action, {"table": "users; DROP TABLE x"})
        assert not result.ok and "identifier" in result.error.lower()
    assert sql_env.db.executed == []


# ── Columns, keys, constraints, indexes, relationships ──────────────────────


COLUMNS = ["name", "type", "nullable", "default_value", "position", "max_length"]


async def test_describe_table_returns_structured_columns_and_keys(pg, sql_env):
    sql_env.db.on(
        "FROM information_schema.columns",
        COLUMNS,
        [
            ["id", "integer", False, "nextval('users_id_seq')", 1, None],
            ["email", "text", False, None, 2, None],
            ["org_id", "integer", True, None, 3, None],
        ],
    )
    sql_env.db.on(
        "FROM pg_constraint con",
        ["name", "type", "columns", "ref_schema", "ref_table", "ref_columns", "definition"],
        [
            ["users_pkey", "p", ["id"], None, None, None, "PRIMARY KEY (id)"],
            [
                "users_org_fk",
                "f",
                ["org_id"],
                "public",
                "orgs",
                ["id"],
                "FOREIGN KEY (org_id) REFERENCES orgs(id)",
            ],
        ],
    )
    out = await pg.execute("describe_table", {"table": "users"})
    assert out.ok and out.data["column_count"] == 3
    assert out.data["columns"][1] == {
        "name": "email",
        "type": "text",
        "nullable": False,
        "default_value": None,
        "position": 2,
        "max_length": None,
    }
    assert out.data["primary_key"] == ["id"]
    assert out.data["foreign_keys"] == [
        {
            "name": "users_org_fk",
            "type": "foreign_key",
            "columns": ["org_id"],
            "references": {"schema": "public", "table": "orgs", "columns": ["id"]},
            "definition": "FOREIGN KEY (org_id) REFERENCES orgs(id)",
        }
    ]
    assert sql_env.db.executed[0][1] == (None, "users")  # schema defaults server-side


async def test_a_table_the_role_cannot_see_looks_like_a_table_that_does_not_exist(pg, sql_env):
    sql_env.db.on("FROM information_schema.columns", COLUMNS, [])
    out = await pg.execute("describe_table", {"table": "payroll"})
    assert not out.ok and out.code is ErrorCode.NOT_FOUND
    assert "cannot see it" in out.error and out.hint


async def test_catalog_queries_filter_by_the_roles_own_privileges(pg, sql_env):
    for action in ("list_constraints", "list_indexes", "table_relationships"):
        await pg.execute(action, {"table": "users"})
    texts = [sql for sql in sql_env.db.user_sql() if "pg_" in sql]
    assert texts and all("has_table_privilege" in sql for sql in texts)
    constraints = next(s for s in texts if "FROM pg_constraint con" in s and "ref_schema" in s)
    # a foreign key never reveals a referenced table the role cannot use
    assert "con.confrelid = 0 OR has_table_privilege(con.confrelid" in constraints


async def test_list_constraints_shapes_every_kind(pg, sql_env):
    sql_env.db.on(
        "FROM pg_constraint con",
        ["name", "type", "columns", "ref_schema", "ref_table", "ref_columns", "definition"],
        [
            ["u_email", "u", ["email"], None, None, None, "UNIQUE (email)"],
            ["c_pos", "c", None, None, None, None, "CHECK (balance >= 0)"],
        ],
    )
    out = await pg.execute("list_constraints", {"table": "users"})
    kinds = {c["name"]: c["type"] for c in out.data["constraints"]}
    assert kinds == {"u_email": "unique", "c_pos": "check"}


async def test_list_indexes(pg, sql_env):
    sql_env.db.on(
        "FROM pg_index ix",
        ["name", "is_unique", "is_primary", "method", "columns", "definition"],
        [
            [
                "users_pkey",
                True,
                True,
                "btree",
                ["id"],
                "CREATE UNIQUE INDEX users_pkey ON users (id)",
            ]
        ],
    )
    out = await pg.execute("list_indexes", {"table": "users"})
    assert (
        out.data["indexes"][0]["columns"] == ["id"] and out.data["indexes"][0]["is_primary"] is True
    )


async def test_table_relationships_cover_both_directions(pg, sql_env):
    cols = ["name", "type", "columns", "ref_schema", "ref_table", "ref_columns", "definition"]
    sql_env.db.on(
        "con.contype::text AS type",
        cols,
        [["fk_org", "f", ["org_id"], "public", "orgs", ["id"], "FOREIGN KEY ..."]],
    )
    sql_env.db.on(
        "AS from_table",
        ["name", "from_schema", "from_table", "columns", "ref_columns"],
        [["orders_user_fk", "public", "orders", ["user_id"], ["id"]]],
    )
    out = await pg.execute("table_relationships", {"table": "users"})
    assert out.ok
    assert out.data["references"][0]["references"]["table"] == "orgs"
    assert out.data["referenced_by"] == [
        {
            "name": "orders_user_fk",
            "from_schema": "public",
            "from_table": "orders",
            "from_columns": ["user_id"],
            "to_columns": ["id"],
        }
    ]


async def test_column_defaults_are_scrubbed_of_secrets(pg, sql_env):
    sql_env.db.on(
        "FROM information_schema.columns",
        COLUMNS,
        [["k", "text", True, "'sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD'", 1, None]],
    )
    out = await pg.execute("describe_table", {"table": "t"})
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in str(out.data)


# ── MySQL: same operations, its own SQL ─────────────────────────────────────


async def test_mysql_lists_tables_with_show_full_tables_and_pages_in_python(my, sql_env):
    rows = [[f"t{i:02d}", "BASE TABLE"] for i in range(30)] + [["v1", "VIEW"]]
    sql_env.db.on("SHOW FULL TABLES", ["Tables_in_app", "Table_type"], rows)
    page = await my.execute("list_tables", {"database": "app", "limit": 10})
    assert (
        page.ok
        and page.data["count"] == 10
        and page.data["truncated"]
        and page.data["next_offset"] == 10
    )
    assert last_sql(sql_env)[0] == "SHOW FULL TABLES FROM `app`"
    views = await my.execute("list_views", {"database": "app"})
    assert [v["name"] for v in views.data["views"]] == ["v1"]
    filtered = await my.execute("list_tables", {"name_like": "T0"})
    assert filtered.data["count"] == 10  # case-insensitive contains


async def test_mysql_rejects_a_bad_database_name(my, sql_env):
    for bad in ("a`b", "bad db", "x;y"):
        assert not (await my.execute("list_tables", {"database": bad})).ok
    assert sql_env.db.executed == []


async def test_mysql_describe_and_constraints_use_bound_parameters(my, sql_env):
    sql_env.db.on("FROM information_schema.columns", COLUMNS, [["id", "int", False, None, 1, None]])
    sql_env.db.on(
        "FROM information_schema.table_constraints",
        ["name", "type", "columns", "ref_schema", "ref_table", "ref_columns"],
        [
            ["PRIMARY", "PRIMARY KEY", "id", None, None, None],
            ["fk_org", "FOREIGN KEY", "org_id,region", "app", "orgs", "id,region"],
        ],
    )
    out = await my.execute("describe_table", {"table": "users", "database": "app"})
    assert out.ok and out.data["primary_key"] == ["id"]
    fk = out.data["foreign_keys"][0]
    assert fk["columns"] == ["org_id", "region"] and fk["references"]["columns"] == ["id", "region"]
    for sql, params in sql_env.db.executed:
        assert "users" not in sql and params == ("app", "users")


async def test_mysql_has_no_separate_schema_listing(my):
    assert "list_schemas" not in {c.action for c in my.capabilities()}


# ── Capabilities are scoped and categorised ────────────────────────────────


def test_catalog_capabilities_are_reads_in_the_schema_category(sql_env):
    pg = sql_env.pg()
    caps = {c.action: c for c in pg.capabilities()}
    schema = [c for c in caps.values() if c.category == "schema"]
    assert {c.action for c in schema} >= {
        "database_info",
        "list_databases",
        "list_schemas",
        "list_tables",
        "list_views",
        "describe_table",
        "list_indexes",
        "list_constraints",
        "table_relationships",
    }
    assert all(c.permission.value == "read" and c.scope == "database.schema.read" for c in schema)
