# ADR-013: Claude Code for engineering, OpenAI for the runtime agent

**Status:** Accepted — runtime-provider portion superseded by [ADR-014](ADR-014-provider-interchangeable-runtime-agent.md); the engineering-agent split below still holds.

## Context

Two distinct systems in this project use language models, and conflating them causes confusion in code, configuration, and credential handling. The engineering control plane builds and maintains the software. The runtime financial agent answers the owner's questions about their money.

## Decision

- **Engineering:** Claude Code as lead orchestrator, with specialist subagents in `.claude/agents/`. Project rules live in `CLAUDE.md`. Reusable workflows in `.claude/skills/`. Mechanical enforcement via `.claude/settings.json` permissions and hooks.
- **Runtime:** OpenAI models power the conversational agent inside the shipped application, per a settled owner decision.

The two never share credentials, context, or configuration.

## Consequences

- `OPENAI_API_KEY` is production runtime secret material. Claude Code never needs it and must never hold it.
- The agent service layer in `src/finance_app/agent/` is written against a narrow internal interface, so the runtime provider stays swappable without touching `analytics/`, `db/`, or the tool definitions.
- Documentation must be explicit about which agent is meant in any given sentence.
- Two providers means two sets of API changes to track.

## Revisit when

The owner decides to move the runtime agent to the Claude API, or a capability gap makes the current split costly. The swap should be contained to `src/finance_app/agent/`.

**Triggered:** the owner asked for the runtime agent to support both providers, not a one-time swap. See ADR-014.
