<div align="center">

<img src="https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/docs/assets/velune-hero.svg" alt="Velune CLI: a faceted three-dimensional V symbol with five agent nodes (Planner, Coder, Reviewer, Challenger, Synthesizer) converging on it" width="100%">

<h1>Velune CLI</h1>

<h3>Your models. Your machine. Your approval.</h3>

<p>An open-source, local-first AI engineer for your terminal.</p>

<p><a href="https://velune-cli.vercel.app/"><strong>Visit the website →</strong></a></p>

<p>
<a href="https://pypi.org/project/velune-cli/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/velune-cli"></a>
<a href="https://pypi.org/project/velune-cli/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/velune-cli"></a>
<a href="https://github.com/Surya-Hariharan/Velune-CLI/actions/workflows/ci.yml"><img alt="CI status" src="https://img.shields.io/github/actions/workflow/status/Surya-Hariharan/Velune-CLI/ci.yml?branch=main&label=CI"></a>
<a href="https://github.com/Surya-Hariharan/Velune-CLI/blob/main/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
<a href="https://github.com/Surya-Hariharan/Velune-CLI"><img alt="GitHub stars" src="https://img.shields.io/github/stars/Surya-Hariharan/Velune-CLI?style=flat"></a>
</p>

<p>
<img alt="Status: beta" src="https://img.shields.io/badge/status-beta-orange">
<img alt="Local-first" src="https://img.shields.io/badge/local--first-yes-brightgreen">
<img alt="No analytics" src="https://img.shields.io/badge/analytics-none-brightgreen">
<img alt="MCP client and server" src="https://img.shields.io/badge/MCP-client%20%2B%20server-blueviolet">
<img alt="Tested on Windows, macOS and Linux" src="https://img.shields.io/badge/CI-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey">
</p>

</div>

---

## Here's the deal.

AI can write code. Fine. The real question is whether you let it near the codebase that pays your bills.

You've got five worries, and they're all fair:

<div align="center">
<img src="https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/docs/assets/velune-questions.svg" alt="Five questions teams ask before letting AI touch a codebase, with Velune's answer to each: where your code goes, who checks the answer, what it may change, which models you can use, and whether it knows your project" width="100%">
</div>

Velune answers every one of them. And every answer points at a file in this repo. Don't take my word for it. Go check.

## What you get

- 🔒 **Your code stays yours.** Run a model on your own machine. No analytics, no usage data sent to us
  ([proof](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/SECURITY.md#zero-telemetry)).
- 🧠 **A council, not a lone gun.** Easy question? One seat. Big refactor? Up to nine seats, including a Challenger
  whose whole job is to poke holes in the plan.
- 🛡️ **You hold the leash.** Manual by default. Every change is a diff you approve. One command to undo.
- 📚 **It remembers.** Five memory tiers per project, so it knows your code before you explain it.

<div align="center">
<img src="https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/docs/assets/velune-council.svg" alt="A matrix of council seats by depth tier. Instant uses the Coder only, minimal adds the Planner, standard adds the Reviewer and Synthesizer, and full uses all nine seats: five core roles and four specialist critics" width="100%">
</div>

<div align="center">
<img src="https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/docs/assets/velune-permissions.svg" alt="A matrix of the Manual, Plan and Auto execution modes against reads, ordinary changes, high-risk changes and changes outside the workspace, showing what is allowed, asked or refused. Manual is the default" width="100%">
</div>

It works like this:

```text
/plan add input validation to signup     it investigates and writes a plan. No file touched.
approve                                  it runs the plan. You see every diff first.
/diff                                    see exactly what changed
/rewind                                  don't like it? Undo the agent's last turn.
```

A council raises your odds of a good answer. It doesn't promise one. It costs more calls than a single model, so it only
shows up when the task needs it. Read your diffs.

## Plugs into everything

<div align="center">
<img src="https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/docs/assets/velune-connect.svg" alt="Velune CLI at the centre, connecting to 18 model providers (4 local and 14 cloud), to opt-in application integrations (Docker, HTTP APIs, PostgreSQL, MySQL and MariaDB, Supabase, background processes), to MCP as both server and client, and to plugins, hooks and custom slash commands" width="100%">
</div>

## The numbers

No hype. Counted from the code. A test recomputes them on every CI run and fails if they drift.

<div align="center">
<img src="https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/docs/assets/velune-numbers.svg" alt="Counted from the repository: 18 provider adapters, 56 slash commands in 12 categories, 5 memory tiers, 9 council seats at full depth, 3 operating systems by 5 Python versions in CI, and 0 analytics services" width="100%">
</div>

The honest part: this is **beta**. Things change between releases. Command execution is process management, not an
OS-level sandbox (a Docker sandbox is opt-in). The full list of limits is in
[SECURITY.md](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/SECURITY.md).

## Get in. Two minutes.

**macOS, Linux, WSL**

```bash
curl -fsSL https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/scripts/install.sh | sh
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/Surya-Hariharan/Velune-CLI/main/scripts/install.ps1 | iex
```

Or `pipx install velune-cli` / `uv tool install velune-cli`. The installer needs no existing Python.

**Updates:** when a new release is out, Velune asks once whether to install it, then updates itself after you quit.
To update by hand, **close every Velune window first**, then re-run the installer (or `velune update`). Replacing a
running Velune can delete its files part-way, so the installer refuses while Velune is open.

Installed with plain `pip` and `velune` says "not recognized" (or "command not found")? The install worked; Python's
Scripts folder just isn't on your `PATH` (pip prints "WARNING: The scripts velune.exe ... are installed in ... which is
not on PATH"). Run `python -m velune doctor --fix`, which works without it and adds the folder to your user `PATH`;
then open a new terminal. Or install with `pipx` (then `pipx ensurepath`).

Then pick your lane:

```bash
# Fully local
ollama pull qwen2.5-coder:7b
cd your-project && velune init && velune

# Cloud (Groq free tier)
cd your-project && velune init --provider groq && velune setup && velune
```

Something off? Run `velune doctor` (or `python -m velune doctor` if `velune` isn't found).

## Join in

One maintainer. Open doors.
[Open an issue](https://github.com/Surya-Hariharan/Velune-CLI/issues/new/choose) ·
[Contribute](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/CONTRIBUTING.md) ·
[Report a vulnerability](https://github.com/Surya-Hariharan/Velune-CLI/security/advisories/new) (privately, never in a public issue).

**Go deeper:**
[Reference](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/reference.md) ·
[Usage guide](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/usage-guide.md) ·
[Slash commands](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/docs/slash-commands.md) ·
[Security](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/SECURITY.md) ·
[Changelog](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/CHANGELOG.md)

---

<div align="center">

Licensed under [Apache 2.0](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/LICENSE). Copyright 2026 Surya HA.
Third-party names belong to their owners and identify services Velune can connect to.
See [NOTICE](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/NOTICE),
[AUTHORS](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/AUTHORS.md) and the
[Code of Conduct](https://github.com/Surya-Hariharan/Velune-CLI/blob/main/CODE_OF_CONDUCT.md).

</div>
