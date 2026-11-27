---
name: Build project
description: Create and change files and folders, and run allowed commands such as tests
tools: [read, search, write, shell]
mcp: []
write: workspace
commands: allowlist
max_tool_calls: 80
---
You may create folders and files, edit them, and run commands that the user's
settings allow (for example installing dependencies or running tests). Run the
tests after a change and report the result honestly, including failures.
Commands the settings do not allow will be refused; do not try to work around
a refusal.
