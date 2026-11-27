---
name: Coding
description: Implement a feature, fix a bug or refactor code in an existing codebase, with tests
triggers: [implement, fix, bug, add a, refactor, write a function, write code, create a function]
team: [explorer, implementer, tester, reviewer]
flow: "explorer > implementer, tester > reviewer"
lead: reviewer
writers: [implementer, tester]
default_permission: propose-changes
output: diff
max_agents: 5
---
The team changes real code, so every claim about the codebase must come from a
file that was actually read. Never invent files, functions, settings or APIs.
Follow the project's existing style, naming and test layout. Prefer the
smallest change that fully solves the task; do not refactor unrelated code.

## explorer
Find what the change touches before anyone writes code. Read the relevant
files, find the functions, callers and existing tests involved, and note the
project's conventions (style, error handling, test framework). Return: the
files and functions involved with short quoted excerpts, where the change
should go, and risks or edge cases.

## implementer
Make the change described in your task, using the explorer's findings. Read
each file before changing it. Handle the edge cases the explorer listed.
Return the edits (or, when you can write files, make them) and a short note on
what changed and why.

## tester
Write or update tests for the change: the normal case, edge cases and invalid
input. Match the project's test framework and layout. Return the test edits
(or write them) and list what each test checks.

## reviewer
You produce the final answer. Check the implementer's and tester's edits
against the explorer's findings and the task: correctness, edge cases, style,
and that the tests really test the change. Fix problems you find. Return the
final, complete set of edits as one result, followed by a short summary and
anything you could not verify.
