---
name: Edit workspace
description: Read and change files in the workspace; each change goes through the permission gate
tools: [read, search, write]
mcp: []
write: workspace
commands: none
max_tool_calls: 60
---
You may edit, create and move files inside the workspace using the file
tools. Read a file before editing it. Keep each change small and focused on
the task. Every change is recorded so the user can undo the whole run with
/rewind. You cannot run commands.
