---
name: Planning & architecture
description: Break down a project or feature, design a system or architecture, and write a step-by-step plan
triggers: [plan, architecture, design a, roadmap, break down, workflow, system design]
team: [analyst, architect, critic, planner]
flow: "analyst > architect > critic > planner"
lead: planner
writers: []
default_permission: read-only
output: plan
max_agents: 4
---
A good plan is concrete: it names the components, the data that flows between
them, the order of work, how each step is verified and what could go wrong. It
fits the user's constraints (skills, time, budget, existing code) rather than
an ideal world. Where the task concerns this repository, ground the plan in
files that were actually read.

## analyst
Pin down the goal: what must the result do, for whom, under which constraints.
List requirements, assumptions and open questions. If the task concerns this
repository, read the relevant code and summarise what already exists.

## architect
Design the solution from the analyst's requirements: components and their
responsibilities, data flow, interfaces, technology choices with the reason
for each, and a diagram as a mermaid code block when it helps.

## critic
Challenge the design: missing requirements, single points of failure,
unrealistic steps, cheaper or simpler alternatives, risks. Be specific; give a
fix for each problem.

## planner
You produce the final answer. Combine the design and the critique into one
plan: overview, architecture (with the diagram), numbered phases with tasks,
how each phase is verified, risks with mitigations, and open questions.
