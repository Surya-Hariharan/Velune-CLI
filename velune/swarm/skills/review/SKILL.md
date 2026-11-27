---
name: Review & audit
description: Review code or a design for bugs, security issues, performance problems and maintainability
triggers: [review, audit, check for bugs, security, vulnerabilit, code quality, performance]
team: [mapper, correctness, security, lead]
flow: "mapper > correctness, security > lead"
lead: lead
writers: []
default_permission: read-only
output: text
max_agents: 5
---
A finding is only useful when it is real and actionable: point to the exact
file and line, quote the code, explain the consequence (what input causes what
wrong result), and suggest a fix. Do not report style preferences as bugs.
Say how confident you are, and separate confirmed problems from suspicions.

## mapper
Map what is being reviewed: list the files and entry points in scope, how data
flows through them, and which parts handle untrusted input, money, files,
credentials or concurrency. Quote the key code.

## correctness
Look for bugs in the mapped code: wrong logic, unhandled errors and edge
cases, broken invariants, race conditions, resource leaks. Report each with
file, line, quoted code, the failing scenario and a fix.

## security
Look for security problems in the mapped code: injection, unsafe file or
command handling, secrets in code or logs, missing authorisation checks,
unsafe deserialisation, weak cryptography. Report each with file, line, quoted
code, how it could be exploited and a fix.

## lead
You produce the final answer. Merge the findings, remove duplicates and
anything not supported by quoted code, and rank them by severity. Return a
table of findings (severity, file:line, problem, fix), then details for the
important ones, then what was not checked.
