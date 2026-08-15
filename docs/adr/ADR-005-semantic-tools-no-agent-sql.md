# ADR-005: Semantic financial tools; no arbitrary agent SQL

**Status:** Accepted

## Context

Giving an LLM a `run_sql(query: str)` tool is the fastest way to make a financial agent feel capable. It also gives a model that reads attacker-influenced strings — merchant names, transaction notes — unbounded read and write authority over the entire financial dataset.

## Decision

The runtime agent gets a fixed vocabulary of semantic, parameterized tools: `get_spending_by_category`, `compare_periods`, `create_budget`, and so on. No SQL tool, no shell tool, no filesystem tool, no URL fetch. The model chooses the business operation; typed Python and parameterized SQL execute it.

## Consequences

- Prompt injection cannot escalate beyond the fixed tool surface, and the tool surface is auditable by reading one registry.
- New capability requires deliberately adding a tool, which is a reviewable event rather than an emergent one.
- Genuinely novel questions may need a new tool. Correct tradeoff.
- Every write tool validates inputs, audits to `agent.tool_calls`, and returns a structured description of the change.

## Revisit when

Never. A test asserts no arbitrary SQL tool exists.
