# ADR-014: Provider-interchangeable runtime financial agent

**Status:** Accepted

## Context

ADR-013 settled OpenAI as the runtime financial agent's model provider and named its own revisit condition: *"The owner decides to move the runtime agent to the Claude API... The swap should be contained to `src/finance_app/agent/`."* The owner has now asked for exactly that, but as a standing capability rather than a one-time migration: the ability to run the runtime agent on **either** OpenAI or the Anthropic Claude API, selected by configuration, not by code change.

This is still a narrow decision. It does not touch:

- **Claude Code as the engineering agent** — unaffected. Claude Code building this application and a Claude-API-model answering the owner's financial questions at runtime remain two different systems, per CLAUDE.md's standing instruction not to conflate them, even though they may now share a vendor.
- **ADR-005** (semantic tools, no arbitrary agent SQL) — the tool boundary is provider-agnostic already and needs no change.
- **ADR-004** (raw Plaid data immutable to the agent) — enforced at the database-role/tool layer, below any provider adapter.

## Decision

The runtime agent is built against a **provider-neutral tool-calling loop interface**, implemented in `src/finance_app/agent/`, with OpenAI and Anthropic as two concrete adapters selected by a `AGENT_PROVIDER` setting (`openai` | `anthropic`).

- **Tool definitions are the single source of truth**, expressed once as plain Python (name, description, JSON Schema, handler) in `src/finance_app/agent/tools/`. Each provider adapter translates that one definition into its own wire format (OpenAI's `tools` array; Anthropic's `tools` array with `input_schema`) — tool definitions are never hand-duplicated per provider.
- **A single `AgentProvider` protocol** (or equivalent minimal interface) owns exactly one responsibility: given the running conversation state and the tool set, make one model call and return either a tool-call request or a final assistant message. Everything else — the tool-execution loop, audit logging to `agent.tool_calls`, the permission/write-boundary checks, conversation persistence — lives above this interface and is written once, not once per provider.
- **System prompts are per-provider**, not shared verbatim. Handoff §8.2's prompting requirements (distinguish known data from interpretation, disclose data gaps, never treat heuristic categorization as certainty, no unlicensed professional advice) are the fixed contract; the literal prompt text tuned to get a given model to reliably honor that contract is expected to differ and is evaluated independently per provider (Milestone 6).
- **Config** gains an explicit provider switch and both credential paths: `agent_provider: Literal["openai", "anthropic"] = "openai"`, plus `anthropic_api_key: SecretStr` and `anthropic_model: str` alongside the existing `openai_api_key`/`openai_model`. Only the credential for the *active* provider is required at runtime; the inactive one may be unset.
- **`agent.tool_calls` audit rows record which provider served each call** (a `provider` column, or equivalent), so audit history stays legible across a provider switch and Milestone 6 evals can be filtered or compared per provider.

## Consequences

- More upfront design work in Milestone 5 than a single-provider implementation: the tool-calling loop must be written against an interface from the start, not extracted from OpenAI-specific code later. This is accepted because the alternative — building OpenAI-specific first and abstracting afterward — has already been flagged once (ADR-013) and the owner has now asked for the general case directly, so building it twice is worse than building it right once.
- Two credential paths in production instead of one: both `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are runtime secret material when both providers are configured, or just the active one's if not. Same handling as any other runtime secret (systemd encrypted credentials, never in the engineering environment) — see ADR-010.
- Two sets of upstream API changes to track instead of one, and two system prompts to maintain and eval instead of one. Milestone 6 (agent evals) must run its golden-dataset assertions against whichever provider(s) are configured for CI, not assume a single fixed provider.
- Feature parity is not guaranteed by construction — if one provider's tool-calling semantics support something the other's don't cleanly (e.g. parallel tool calls, structured-output guarantees), the `AgentProvider` interface targets the intersection of what both need for this application's tool set, not the maximum of either provider's capability.
- Naming risk: because the engineering agent (Claude Code) and a Claude-API-powered runtime agent can now coexist in one running system, logs, docs, and code comments must keep saying *which* agent they mean rather than relying on "Claude" being unambiguous. CLAUDE.md's existing "these are different systems, do not conflate them" instruction now carries more weight, not less.

## Revisit when

A third provider is wanted (extend the adapter set, no interface change expected), or the `AgentProvider` interface proves too narrow for a capability only one provider offers and the owner decides that capability is worth a provider-specific escape hatch.
