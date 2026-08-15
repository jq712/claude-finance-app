# ADR-006: Deterministic financial arithmetic

**Status:** Accepted

## Context

LLMs produce plausible arithmetic. In a system whose entire purpose is telling someone the truth about their money, plausible is worse than useless, because it is confidently wrong and unverifiable after the fact.

## Decision

All financial computation — sums, averages, medians, percentage change, budget variance, cashflow, savings rate, rolling averages — happens in SQL or Python. The model receives compact structured results and explains them. It never calculates. Money is `NUMERIC`/`DECIMAL` or integer minor units; never binary floating point.

## Consequences

- Every figure the agent reports is reproducible and traceable to a query.
- The application stays fully useful with the runtime LLM provider unavailable, whichever one is configured (ADR: see Milestone 4 exit criteria; ADR-014).
- Agent evals can assert exact numeric equality against a golden synthetic dataset.
- Reviewers have one clear smell to watch for: any prompt asking the model to compute.

## Revisit when

Never.
