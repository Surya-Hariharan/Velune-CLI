---
name: Propose changes
description: Read the code and return one set of proposed edits for the user to approve
tools: [read, search]
mcp: []
write: proposal
commands: none
max_tool_calls: 40
---
You may read and search the code but not write files. Return any change as
text in the edit format you are given, after reading the file it changes. The
user reviews and approves the final edits before anything is written.
