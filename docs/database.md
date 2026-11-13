# Database integrations

Velune can inspect and, with your approval, change **PostgreSQL**, **MySQL / MariaDB** and **Supabase**
databases through the same integration runtime as Docker and HTTP: operations are discovered with
`integrations_list` / `integrations_enable`, run through the permission gate, and return bounded, redacted,
structured results. There is no separate database framework: the three connectors share one core
(`velune/resources/sql_base.py`, `connections.py`, `sql_parse.py`, `sql_results.py`) and differ only in their
driver and their catalog SQL.

Model-written SQL is treated as **untrusted input**. The model never receives a password, never splices values
into SQL, never sends two statements at once, and can only change data with an explicit, approved `execute`.

## Install and connect

```text
pip install "velune-cli[postgres]"     # psycopg 3 (psycopg2 is used if that is what is installed)
pip install "velune-cli[mysql]"        # pymysql
/resource configure postgres           # a connection named "postgres" (the default)
/resource configure postgres staging --env staging --read-only      # a second, named connection
/resource configure postgres prod --allow-remote --tls verify-full  # a remote server (see below)
/resource configure supabase           # Supabase needs no driver beyond httpx
```

`/resource configure` asks for host, port, database, user and password and stores **all of it together in the
encrypted keystore** (`resource:<type>:<name>`) — the same store, key and cipher as provider keys; nothing new.
Options go on the command line, so the questions stay the same: `--env <label>` (an environment label such as
`staging` or `production`), `--read-only` (refuse every statement that changes anything) and, for a remote
server, `--allow-remote [--tls require|verify-ca|verify-full]`.

The agent refers to a connection by **name**. It never sees the host's credentials; `list_connections` shows
name, host, database, environment and state only.

### Local by default; remote only with TLS

These connectors hold passwords, so a non-local host is refused unless the **stored** connection says
`allow_remote` and requires TLS (`sslmode` of `require`, `verify-ca` or `verify-full`; `verify-full` is the
recommended choice). You grant it with `/resource configure <engine> <name> --allow-remote [--tls <mode>]`
(default `verify-full`); without the option a non-local host is saved but refused when connecting, and
`/resource configure` says how to allow it. A repository's `velune.toml` cannot grant it.

### Configuration

```toml
[resources.postgres]
enabled = true
default_connection = "dev"        # used when an operation names none (empty: the only one)
connect_timeout = 5               # seconds
statement_timeout = 30            # seconds; set on the server and enforced by the client
idle_transaction_timeout = 60     # an idle transaction is rolled back after this
max_rows = 200                    # rows returned per query (the model may only lower it)
max_cell_chars = 500
max_result_chars = 20000
redact_columns = ["ssn", "card_*"]   # extra column-name patterns masked in every result

[resources.postgres.connections.staging]
credential = "pg-staging"         # which stored credential to use (default: the connection's own name)
environment = "staging"           # a label; "production" makes every change say so
read_only = true                  # refuse every statement that changes anything
statement_timeout = 10
max_rows = 100
```

A connection entry holds **no password and, normally, no endpoint**. Host, port, user, database and password
live together in the keystore, so a repository cannot point a stored password at another server. An endpoint in
configuration is honoured only in a trusted workspace and only when *no* credential is stored (there is then no
password to protect). Naming a *different* stored credential (`credential = "..."`) is likewise honoured only in a
trusted workspace, and an environment label stored with the credential wins over configuration, so a repository can
make a connection stricter (`read_only`, a label on an unlabelled connection) but cannot relabel a production one.
`[resources.mysql]` and `[resources.supabase]` take the same settings.

## What each integration can do

| Capability | PostgreSQL | MySQL / MariaDB | Supabase |
| --- | :---: | :---: | :---: |
| `connections`: `list_connections`, `health`, `diagnose` | ✓ | ✓ | `list_connections`, `verify` |
| `schema`: `database_info`, `list_databases`, `list_tables`, `list_views`, `describe_table`, `list_indexes`, `list_constraints`, `table_relationships` | ✓ | ✓ | `list_tables`, `inspect_schema` (PostgREST schema) |
| `schema`: `list_schemas` | ✓ | — (a schema is a database) | — |
| `query_read`: `query`, `explain` | ✓ (SQL) | ✓ (SQL) | `query` (REST table read; RLS applies) |
| `query_write`: `execute` | ✓ | ✓ | — |
| `transactions`: `transaction_begin`, `_execute`, `_commit`, `_rollback` | ✓ | ✓ | — |
| `storage`, `functions` | — | — | `storage_buckets`, `edge_functions` |
| privileged | — | — | `rls_policies`, any call with `service_role` |

Supabase does **not** open a PostgreSQL connection: SQL against a Supabase database belongs to the `postgres`
integration (configure its connection string with TLS), so nothing is duplicated. Enable only the categories a
task needs, for example `integrations_enable postgres categories=["schema","query_read"]`; the rest stay
reachable through `integration_call`.

## Schema discovery

Catalog operations return **bounded, structured, paged** JSON, never a dump of the database:

```json
{"ok": true, "action": "list_tables", "data": {"connection": "dev", "tables": [{"schema": "public", "name": "orders", "kind": "table"}],
 "count": 1, "offset": 0, "truncated": false}}
```

* `list_tables` / `list_views` take `schema` (`database` on MySQL), `name_like` (matches containing text; `%` and
  `_` are escaped), `limit` (≤ 200) and `offset`; a truncated page says `next_offset`.
* `describe_table` returns columns (name, type, nullable, default, position, length), the **primary key** and the
  **foreign keys**; `list_constraints` adds unique and check constraints; `list_indexes` the indexes;
  `table_relationships` the foreign keys out of a table and the tables that reference it.
* **Only what the connected role can use is returned.** PostgreSQL catalog queries filter with
  `has_table_privilege` / `has_schema_privilege` / `has_database_privilege`, and a foreign key never reveals a
  referenced table the role cannot use; MySQL's `information_schema` and `SHOW` are privilege-filtered by the
  server. A table the role cannot see is reported exactly like one that does not exist.
* Every value (schema, table, pattern, page) is a **bound parameter**. Identifiers (letters, digits, `_`, `$`)
  are validated before use; the one place a name is written into SQL (MySQL `SHOW … FROM`) is back-quoted after
  that validation.

## Running queries

`query` runs **one** read-only statement; `execute` runs **one** statement that changes data or schema.

```text
postgres__query   {"sql": "SELECT id, email FROM users WHERE id = %s", "params": [42]}
postgres__execute {"sql": "UPDATE users SET active = %s WHERE id = %s", "params": [false, 42]}
```

* **Values go in `params`**, bound by the driver to `%s` placeholders (use `%%` for a literal `%` when `params`
  are given). The text is never assembled from model-supplied values.
* **Exactly one statement.** The text is tokenized (strings, comments, `$$`-quoting and quoted identifiers are
  understood), split into statements and counted; two statements, none, or text that cannot be parsed are
  refused before anything reaches the server. A hidden second statement in a comment or string is not a second
  statement, and one in a comment the *engine* might treat as code is counted.
* **Reads are read-only at the engine.** `query` runs inside `SET TRANSACTION READ ONLY` (PostgreSQL) /
  `START TRANSACTION READ ONLY` (MySQL), is always rolled back, and PostgreSQL streams rows from a server-side
  cursor so a huge `SELECT` is never loaded whole. Classification is not the only guard.
* **Classification is structural, not a keyword guess**: `WITH x AS (DELETE …) SELECT`, `EXPLAIN ANALYZE
  DELETE …` (which *runs* the delete), `SELECT … INTO`, `SELECT … FOR UPDATE`, `nextval()`, and functions such as
  `pg_terminate_backend()` / `set_config()` are all *not* reads. `UPDATE`/`DELETE` without a `WHERE` is called
  out in the approval ("affects every row").
* **Administrative statements are never run, in any mode**: `GRANT`/`REVOKE`, `SET`/`RESET` (session state),
  `CREATE`/`ALTER ROLE|USER|EXTENSION|SYSTEM`, `COPY`, `DO`, `PREPARE`/`EXECUTE`, `LOAD`, `KILL`, `USE`, `LOCK`,
  and transaction-control statements outside the transaction operations. They are blocked, not merely gated.
* **Bounded results.** At most `max_rows` rows (default 200) with `truncated: true` and the reason when cut; long
  cells are shortened; total output is capped. Dates, decimals, UUIDs and binary become plain JSON; values in
  columns named like `password`, `secret`, `token`, `api_key`, … (and your `redact_columns`) are masked, and
  secret-shaped text elsewhere is scrubbed.
* **Timeouts and cancellation.** `statement_timeout` is set on the server and enforced by the client; a call may
  lower it with `timeout_seconds`. On timeout (or if the task is cancelled) Velune asks the server to cancel the
  statement (a cancel request on PostgreSQL, `KILL QUERY` on MySQL) and drops the connection so nothing is left running or leaked.
* **Errors are normalized** to `connection_failed`, `authentication_failed`, `authorization_denied`, `not_found`,
  `validation_failed`, `conflict`, `timeout` and `adapter_failure`, with a hint; only the driver's primary message
  is kept (`DETAIL` lines can echo row values), and passwords are scrubbed.
* Results carry `statement`, `connection`, `duration_ms`, `rows_affected` (writes) and `warnings`.

## Transactions

```text
postgres__transaction_begin   {}                                   → {"transaction_id": "3f9c…"}
postgres__transaction_execute {"transaction_id": "3f9c…", "sql": "UPDATE accounts SET balance = balance - %s WHERE id = %s", "params": [10, 1]}
postgres__transaction_execute {"transaction_id": "3f9c…", "sql": "INSERT INTO ledger (account, amount) VALUES (%s, %s)", "params": [1, -10]}
postgres__transaction_commit  {"transaction_id": "3f9c…"}
```

* Nothing is saved until `transaction_commit`; `transaction_rollback` discards everything. Each statement is
  authorized **separately** (approving `transaction_begin` approves nothing inside it).
* One open transaction per connection; other one-shot calls on that connection are refused until it ends.
* **A failed statement poisons the transaction**: further statements and `transaction_commit` are refused
  ("only `transaction_rollback` is possible"), so a failure can never be committed by accident.
* A lost connection loses the transaction, and the result says **nothing was committed**. If the connection
  drops *during* `COMMIT` the outcome is genuinely unknown and the result says so.
* A transaction idle longer than `idle_transaction_timeout` is rolled back automatically (PostgreSQL also sets a
  server-side `idle_in_transaction_session_timeout`); disconnect rolls back anything open.
* `transaction_begin` with `read_only: true` opens a read-only transaction (no changes possible).
* Independent operations are **not** atomic: only statements inside one transaction are.

## Permissions

| Operation | MANUAL | PLAN | AUTO |
| --- | --- | --- | --- |
| schema and connection reads, `health`, `diagnose`, `query`, `explain` | allow | allow | allow |
| `transaction_begin` with `read_only` / `transaction_rollback` | allow | allow | allow |
| `execute` data change (`INSERT`/`UPDATE`/`DELETE`) — scope `database.query.write` | ask | **deny** | **ask** |
| `execute` schema change (`CREATE`/`ALTER`) — `database.schema.write` | ask | **deny** | **ask** |
| `execute` destructive (`DROP`/`TRUNCATE`/`ALTER … DROP`/disable RLS) — `database.query.destructive` | ask | **deny** | **ask** |
| writable `transaction_begin`, `transaction_commit`, writes inside a transaction — `database.transaction` | ask | **deny** | **ask** |
| administrative statements, several statements, unparseable text, a write through `query` | **deny** | **deny** | **deny** |
| any change on a `read_only` connection | **deny** | **deny** | **deny** |

* **AUTO never changes a database unattended**, and no connection is assumed disposable because it is named
  `dev` or `test`: a changing operation is high-risk, so it asks and an "always allow" answer is not honoured. A
  `production` or remote connection says so in the approval.
* The approval shows the target and the statement: `connection=staging db=app host=localhost env=staging |
  UPDATE accounts SET balance = %s WHERE id = %s  [2 bound values]` — never a password, never the bound values.
* **PLAN executes no mutation** and explains the denial ("record this step with `write_plan`").
* **Discovery grants nothing.** `integrations_list` / `integrations_enable` only expose tool definitions; every
  call still passes through `authorize_and_execute`. Database-native permissions are never bypassed: Velune
  connects as the role you stored and never issues `SET ROLE`, `SET SESSION AUTHORIZATION` or a privilege change.

## Supabase

* **Row Level Security applies.** Reads use the project's *anon* key, so RLS decides what comes back.
* **The service-role key is never shown to the model.** `service_role: true` (and `rls_policies`) use it to
  *bypass* RLS; that is a privileged act: it is a high-risk, secret-touching action that **asks in every mode,
  is denied in PLAN and is never covered by a session grant**.
* `query` takes `table`, `select`, `limit`, `offset`, `order`, one `filter_*` triple (`eq neq gt gte lt lte like
  ilike is`); the table, column and `select` are validated, the project host is the only host ever contacted, and
  rows are bounded and column-redacted like SQL results. HTTP failures map to `authentication_failed` (bad key),
  `authorization_denied` (RLS or grants — the hint says so), `not_found`, `validation_failed`, `timeout`, …
* Several projects can be configured by name, each with its own URL and keys.

## Using it well: model-facing workflows

*Find out what is there* (cheap, no approval): `integrations_enable postgres categories=["schema"]` →
`list_tables` → `describe_table {"table": "orders"}` → `table_relationships {"table": "orders"}`.

*Answer a question*: add `query_read` → `query {"sql": "SELECT status, count(*) FROM orders GROUP BY status"}`.

*Diagnose*: a failing call returns a code and hint (`authentication_failed: Check the stored username and
password`); `diagnose` walks driver → configuration → TLS policy → reachability → login → query and stops where
it fails.

*Change data*: `execute` for one statement, or `transaction_begin` → several `transaction_execute` →
`transaction_commit`. Each step asks.

*Work across environments*: `list_connections` shows `dev`, `staging`, `prod`; name one with `connection`.
Operations on an ambiguous default are an error, not a guess.

## Testing

The whole suite runs offline against an in-memory fake server (`tests/sql_fakes.py`), a fake keystore and
`httpx.MockTransport`:

```text
python -m pytest tests/test_sql_*.py tests/test_supabase_connector.py
VELUNE_SQL_LIVE=1 VELUNE_LIVE_PG="localhost:5432:postgres:postgres:pw" python -m pytest tests/test_sql_live.py  # optional
```

The optional live tests need a **throwaway** local server and only use temporary tables inside rolled-back
transactions.

## Limitations

* No live server was exercised by the automated suite; the real drivers' read-only transactions, server cursors,
  cancellation and the catalog SQL are covered by the optional live tests.
* MySQL has no server cursor here: a large `SELECT` is bounded by `sql_select_limit`, the execution-time limit and
  the fetch cap, not by streaming. PostgreSQL streams with psycopg 3 only (psycopg2 buffers).
* The SQL classifier is structural but is not a full parser of either dialect; anything it cannot confirm is not
  a read, and the engine-level read-only transaction is the backstop. `SET`, `COPY`, `DO` and similar are
  blocked rather than analysed.
* Row-level locks (`SELECT … FOR UPDATE`) are classified as changes. Stored procedures (`CALL`) are changes.
* A connection's timeouts rely on server settings (`max_execution_time` on MySQL 5.7.8+, `max_statement_time` on
  MariaDB); where unsupported the client-side deadline and cancellation still apply.
* Only one transaction per connection, and a transaction cannot span connections.
* SQLite, MongoDB, Redis and other engines are not supported.
