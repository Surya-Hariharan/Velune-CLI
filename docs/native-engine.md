# Native orchestration engine (`velune native`)

> **Status: experimental, opt-in, off by default.** This page separates four things: what the command
> does today, what the tests guarantee, what is known to be limited or unverified, and what is only a
> proposal. Nothing under "Proposed" exists.

## 1. Using it today

`velune native` runs **one task** as **one read-only agent**. The agent may call a single tool,
`read_file`, and every call is shown to you and runs only if you answer yes. An opt-in
`--plan-review` mode (section "Plan/review mode") adds a plan and a review around that agent.

The engine is off until you turn it on. In Windows PowerShell:

```powershell
$env:VELUNE_ORCHESTRATION__ENGINE = "on"
velune native "Read README.md and summarize it" --provider groq --model <model-id>
```

This sets the variable for the current PowerShell session only. To turn it off again:

```powershell
Remove-Item Env:VELUNE_ORCHESTRATION__ENGINE
```

bash/zsh equivalent: `export VELUNE_ORCHESTRATION__ENGINE=on` and `unset VELUNE_ORCHESTRATION__ENGINE`.

### Options

| Option | Meaning |
| --- | --- |
| `TASK` (argument) | What you want done. Empty, or longer than 4000 characters, is rejected. |
| `--provider`, `-p` | Provider id. Default: the provider of your saved active model (set with `/model use`). |
| `--model`, `-m` | Model id. Default: your saved active model, **only** if it belongs to the same provider. |
| `--timeout` | Whole-run time limit in seconds. Default `120`, allowed range `1`–`3600`. |
| `--plan-review` | Opt-in. Plan, execute and review (at most one revision) instead of one agent. Off unless given. |

There are no other options. `--json` and `--yes` are the app's global options and go before the command
(`velune --json native ...`); see "Approval" for what `--yes` does here.

### Plan/review mode (opt-in, experimental)

```powershell
velune native "Read README.md and summarize it" --provider groq --model <model-id> --plan-review
```

* **What runs:** a planner writes a short plan; an executor carries it out (the only step that may call
  `read_file`, with the same per-call approval as above); a reviewer checks the draft and answers
  `VERDICT: APPROVE` or `VERDICT: REVISE`. On REVISE the executor revises once; a second REVISE ends the
  run as "not approved within the allowed revisions". Nothing is skipped silently: the final line says
  the run completed, how many drafts and model calls it used.
* **Same model throughout.** The one provider and model you chose do all three steps. The review is a
  second pass by the same model, not an independent check, and the reviewer sees the plan and the draft
  but not tool output. The output says so. The review prompt asks the model to challenge the draft
  rather than approve it, but that is an instruction to a model, not a guarantee.
* **One control for the whole run.** A single run control (created before the first planning request)
  holds the time limit, the request budget and the cancellation flag for planner, executor and
  reviewer alike. The time limit does not restart between steps, every provider request reserves one
  attempt on one shared budget (defaults: 24 model calls, 48 provider attempts, 32 tool calls), and a
  provider call is cut off at the time that remains. Each provider call is tried once; the command
  has no retry loop of its own and no fallback to another provider or model.
* **Distinct endings** (all but cancellation exit `1`): completed (exit `0`); time limit reached;
  request budget used up; a tool call not approved; the reviewer never approved; a provider or model
  error. Ctrl-C is a cancellation (exit `130`). After any ending no further provider request or tool
  call starts. A tool call that was already running cannot be interrupted; if one was, the output says
  it may still have been running.
* **Output:** the final answer and one status line. The plan and the review text are not printed.
  `--json` prints one object with `mode`, `status`, `answer`, `error`, `failure_kind` and counts.
* **Tools:** unchanged. Planner and reviewer get no tools; a reply from either that contains a tool call
  fails the run and nothing is executed.
* The driver (`PlanReviewDriver`) is the only writer of run artifacts; they live in memory for the run and
  are not saved. There is no fallback from this mode to the single agent, normal chat or the Council.

### Enabling it: configuration and precedence

The flag is `orchestration.engine`, values `"off"` (default) or `"on"`. Any other value is treated as
`"off"` and logs a warning. It can be set two ways:

```toml
# velune.toml
[orchestration]
engine = "on"
```

or the environment variable `VELUNE_ORCHESTRATION__ENGINE` (also readable from a `.env` file in the
working directory, like every `VELUNE_` setting).

**Precedence (checked by loading real config files, not assumed):** a value written in `velune.toml`
**wins over the environment variable**. With `engine = "on"` in the file, setting the variable to `off`
still leaves it on; with `engine = "off"` in the file, setting the variable to `on` still leaves it off.
The environment variable takes effect only when `velune.toml` does not set `orchestration.engine`
(including when the file has no `[orchestration]` section). To disable the engine, remove or change the
file setting; unsetting the variable is not enough if the file enables it.

The config file searched is `velune.toml` in the workspace, then (via the loader's fallback) a
`velune.toml` in a parent directory or `~/.velune/velune.toml`. `velune config show` reports which file
was read.

### When the flag is off

The command prints a message explaining how to enable it, exits with status `1`, and does nothing else:
no provider lookup, no model call, no tool. It never falls back to normal chat, the Council, another
provider or another model.

### Choosing a provider and model

Both are required and nothing is guessed. If either is missing and there is no usable saved choice, the
command prints "Choose a provider and model…" and exits `2`. The provider is looked up once by exactly
the id given; if it is unavailable the run stops with an error rather than using a different one.

### Approval

* Each `read_file` call is shown as plain text (control and terminal-escape characters are replaced) and
  needs an explicit **y** at an interactive terminal. Enter, **n**, Esc, a timeout (60 s per prompt),
  end of input, an error, or no terminal all mean **no**.
* An approval covers **that one call** (bound to the tool, its exact arguments and an invocation id),
  is single-use, and expires after 120 s. There is no "always allow", no session grant and no remembered
  answer; the same call requested again prompts again.
* The global `--yes` flag does **not** approve tool calls here. `--json` mode and non-interactive
  terminals never prompt, so every tool call is denied.

### Cancellation, timeouts, exit codes

| Situation | Behaviour | Exit |
| --- | --- | --- |
| Run completes | answer printed (JSON object with `--json`) | `0` |
| Run fails (denied approval, provider error, deadline, budget, …) | fixed one-line message | `1` |
| Flag off | enable-instructions message | `1` |
| No provider/model chosen | usage message | `2` |
| Ctrl-C at an approval prompt, or Ctrl-C during the run | the call is not approved, the run is marked cancelled so nothing further starts, "Cancelled. No further requests were made." | `130` |

The `--timeout` deadline is a failure (exit `1`), not a cancellation. Esc at a prompt is an ordinary
denial, not a cancellation.

### Errors and secrets

Failures are reported as fixed messages. Exception text, provider response bodies and credentials are not
printed. One credential-aware scrubber (exact values held in the credential store and in provider
environment variables, plus common key patterns) is applied to text sent to the provider, the approval
display, tool output (before it is truncated), model text and the final answer. If the scrubber cannot be
built, the run does not start.

## 2. What is connected today

```
velune native            velune/cli/commands/native.py          flag check, option parsing, prompts, exit codes
  ├─ run_native_task     velune/orchestration/engine_adapters/native_session.py        (default: one agent)
  └─ run_native_plan_review  (same module; only with --plan-review)
       ├─ PlanReviewDriver / create_plan_review_driver   engine/driver.py, engine/entry.py
       ├─ ReasoningAgentRunner + ProviderReasoningInvoker  engine/runner.py, engine_adapters/reasoning_invoker.py
       │    └─ ControlLimits (a view of the run's one RunControl)   engine/limits.py
       └─ plan_review_prompts / plan_review_agents       engine/native_task.py
  both modes share:
       ├─ read_only_agent / task_input   velune/orchestration/engine/native_task.py
       ├─ build_acting_stack             velune/orchestration/engine_adapters/acting_stack.py
       │    ├─ ProviderActingInvoker     engine_adapters/provider_acting.py   (the provider adapter)
       │    ├─ TrustedToolExecutor       engine/toolexec.py                   (the only code that runs a tool)
       │    ├─ ActingAgentRunner         engine/acting.py
       │    └─ credential_scrubber       engine_adapters/secrets.py
       ├─ InteractiveApprovalPort        engine_adapters/approval_port.py     (asks you)
       │    └─ ApprovalLedger            engine/approval.py                   (binds, spends once, expires)
       └─ RunControl / ExecutionBudget   engine/execcontrol.py                (wall clock, cancellation)
```

* **Flag checked in four places:** `native_command` (so the disabled path does nothing),
  `run_native_task` and `run_native_plan_review` (defence in depth), and `create_plan_review_driver` in
  `engine/entry.py`. No other code reads `orchestration.engine`; a test enforces that only
  `velune/cli/commands/native.py` outside the engine, state and adapter packages imports the engine.
* **Approval owner:** `TrustedToolExecutor` asks the port, and `ApprovalLedger` validates and spends it.
  The CLI prompt (`confirm_ask`, the existing prompt widget) only relays y/n.
* **Tool:** `ReadFile` from the existing tool registry, run through the existing permission entry point
  under a headless gate, so anything that would need to *ask* about a path outside the workspace is
  refused.
* **Provider access:** the existing `ProviderRegistry` (`runtime.provider_registry`) and the existing
  provider objects. No new provider code runs in this command.
* **Output ownership:** the command prints the scrubbed answer to the console. It writes no files and
  persists nothing. Run artifacts (the engine's `ArtifactStore`) are written only by `PlanReviewDriver`,
  which only `--plan-review` uses; the CLI and session layers read its result and never touch artifacts
  or run state (a test scans for this).
* **Run control owner:** `run_native_plan_review` creates the one `RunControl`, before any planning
  request, and passes it to the driver, which hands it to the acting runner; the reasoning invoker gets a
  view of the same control. The driver creates its own control only when a caller supplies none (as in
  its unit tests).

### Still isolated

`chat`, the REPL, `ask`, `run`, the Council and MCP never read the flag and never import the native
modules (checked by a subprocess import test and by the absence of changes under `velune/providers`,
`velune/council`, `velune/cognition`). `PlanReviewDriver` is reachable only through
`velune native --plan-review`.

## 3. Tested guarantees versus unverified assumptions

**Tested** (offline, with fake providers; `tests/test_engine_cli_integration.py`,
`tests/test_engine_plan_review_session.py`, `tests/test_engine_plan_review_cli.py` plus the engine
suites):
default-off and no side effects while off; no provider fallback; explicit and saved provider/model
pairing including legacy ids; y/Y approve and n/N/Enter/Esc/Ctrl-C never approve (driven through the real
prompt widget with pipe input); `--yes` and `--json` cannot approve; single-use approvals; sanitized
prompt text; secrets never reach the prompt, provider requests or output; fixed error messages;
Ctrl-C → exit 130 with the provider seeing exactly one request; deadline ≠ cancellation; no prompt_toolkit
application left running after a timeout or task cancellation.

For `--plan-review`: one run control shared by driver, acting runner and reasoning invoker; planner,
executor turns and reviewer all counted on one ledger without double counting; a spent budget or time
limit sends no further request; the time limit starts before planning and does not restart; deadline and
cancellation stay distinct; planner and reviewer are offered no tools and a tool call in their replies
is rejected; secrets in model artifacts do not reach later requests or the answer; a denied or
cancelled approval runs no tool; `--plan-review` is rejected by `chat`, `ask` and `run`.

**Not verified:**

* Terminal raw-mode restoration on a real console. Tests use a pipe input, which has no terminal mode to
  restore; that cleanup belongs to prompt_toolkit and was not exercised in a real terminal.
* A real `read_file` call outside the workspace through `velune native`. Refusal of outside-workspace
  reads is tested for the executor with a stand-in tool, not with `ReadFile` end to end.
* Behaviour against every provider. One live smoke test ran earlier against Groq during development, for
  the single-agent mode only. **`--plan-review` has never been run against a real provider**; its tests
  are offline.
* Whether real models follow the plan/review prompts (for example, begin the review with a `VERDICT:`
  line). A model that does not makes the run fail with "no usable verdict".
* Scrubbing is pattern and exact-value matching. It cannot catch encoded, split, hashed or very short
  (under 8 characters) secrets, or secrets the process does not hold, such as ones a tool read from a file.

## 4. Known limitations

* One tool (`read_file`); no writes, no shell. The default mode is one agent; `--plan-review` adds a
  plan and a review, at most one revision, all by the same model.
* The time limit is cooperative for a tool call already running: it is not interrupted, only reported.
* Each provider call is tried once (no retries), so a transient provider error ends the run.
* Plan/review needs a provider that reports a native stop reason for tool calls (Groq does; others may
  refuse tool proposals), and it makes several model calls per task, so it costs more than the default.
* Opt-in and experimental; not the default chat experience and not production-ready.
* Normal application startup builds the provider registry for every command, including a disabled
  `velune native`. No provider instance, tool or network call is created by the disabled command.
* A `velune.toml` setting overrides the environment variable (see above), which can surprise users who
  expect the variable to win.
* The CLI integration tests are slow (about two minutes) because each boots the full runtime.

## 5. Proposed future work (not implemented, not scheduled)

Each item needs its own explicit approval before any work starts.

1. Let planner and reviewer use different providers or models (an independent review), which needs a
   role-to-model mapping.
2. Decide whether to persist run artifacts or print the plan and review on request.
3. Optional bounded provider retries under the shared budget.
4. A feature-flagged evaluation and regression strategy, including a live run, before anything else
   consumes the engine.
5. Only after the above and explicit approval: controlled integration with normal chat or the Council.
