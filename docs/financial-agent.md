# The runtime financial agent (Milestone 5)

`finance chat` is a conversational interface to the same deterministic analytics layer
`src/finance_app/analytics/` already exposes through the `finance` CLI (see
[`docs/cli.md`](cli.md)). The model chooses *which* business question to ask; typed Python and
SQL answer it. See [ADR-014](adr/ADR-014-provider-interchangeable-runtime-agent.md) for the
design decision this implements and handoff §8/§8.4 for the source requirements.

**This is not Claude Code.** Claude Code is the engineering agent building this application. The
runtime financial agent is a separate system the *owner* talks to about their own data, even when
`AGENT_PROVIDER=anthropic` puts a Claude-family model on both sides. Logs, docs, and code must
keep saying which agent they mean — see CLAUDE.md.

## Layering

```text
CLI ("finance chat") / conversation state       agent/conversation.py, cli/main.py
    -> agent loop (provider-agnostic)           agent/loop.py
       - loops until the model stops calling tools
       - executes + audits every tool call (agent.tool_calls)
    -> AgentProvider protocol                   agent/providers/base.py
       run_turn(system_prompt, messages, tools) -> ToolCallRequest | FinalMessage
    -> concrete adapter                         agent/providers/openai.py
                                                 agent/providers/anthropic.py
    -> the provider's own SDK / wire format
```

`AgentProvider` is the entire seam: one model call in, one decision out. Everything else —
looping, auditing, conversation persistence, write-tool bookkeeping — is written once, in
`agent/loop.py`/`agent/conversation.py`, and is identical regardless of which adapter is active.

## Provider switch

```bash
AGENT_PROVIDER=openai      # or anthropic — default is openai
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o        # optional; adapter default otherwise
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-4-5
```

Only the active provider's key is required. `finance chat` validates this at startup
(`Settings.active_agent_credential()`) and fails with a clear message — never a bare `KeyError`
mid-conversation. Switching providers is a config change only: no code change, no tool
redefinition (see `tests/unit/test_agent_config.py`, `tests/unit/test_agent_providers.py`).

System prompts are per-provider (`agent/prompts.py`), not shared text — both satisfy the same
fixed behavioral contract (handoff §8.2) but are tuned/evaluated independently per provider
(Milestone 6).

## The tool set

Defined once, as plain Python, in `agent/tools/`: a name, a description, a JSON Schema for
inputs, and a handler. Each adapter translates that single definition into its own wire format at
call time — OpenAI's `tools`/`function` shape, Anthropic's `tools`/`input_schema` shape. A tool is
added in exactly one place and both providers pick it up.

**Read tools** (`agent/tools/read.py`) — thin wrappers over `analytics/*.py`; no tool computes a
sum, average, or comparison itself:

| Tool | Analytics function |
| --- | --- |
| `get_transactions` | `analytics.transactions.list_transactions` |
| `search_transactions` | `analytics.transactions.search_transactions` |
| `get_spending_summary` | `analytics.spending.get_spending_summary` |
| `get_spending_by_category` | `analytics.spending.get_spending_by_category` |
| `get_category_summary` | `analytics.spending.get_spending_by_category` (reshaped) |
| `get_income_summary` | `analytics.income.get_income_summary` |
| `compare_periods` | `analytics.spending.compare_periods` |
| `calculate_cashflow` | `analytics.cashflow.calculate_cashflow` |
| `get_budget_status` | `analytics.budgeting.get_budget_status` |
| `find_recurring_transactions` | `analytics.recurring.find_recurring_transactions` |

**Write tools** (`agent/tools/write.py`) — every one validates its inputs, writes only
`user.*`/`finance.*`, and returns a structured description of the change:

`set_transaction_category`, `clear_transaction_category_override`, `add_transaction_note`,
`add_transaction_tag`, `remove_transaction_tag`, `create_budget`, `update_budget`,
`archive_budget`, `update_user_preference`.

There is no `run_sql`, no shell tool, and no filesystem access, and there never will be —
`tests/security/test_role_grants.py::test_no_arbitrary_sql_tool_exists_in_the_agent_tool_registry`
fails loudly if any tool in the registry ever grows a `query`/`sql`/`statement` parameter.

## The write boundary is enforced twice

1. **Database grants.** The agent's own connection (`agent/db.py`, `AGENT_DATABASE_URL`) is
   always the `finance_agent` role — SELECT-only on `plaid.*`, read/write on
   `user.*`/`finance.*`/`agent.*`, no access to `ops.*` at all. This is the same role
   `tests/security/test_role_grants.py` already proves the grants for; `finance chat` never uses
   the `finance_app`/`finance_owner` connection `db/session.py` provides to the rest of the
   application. See `tests/security/test_agent_db_boundary.py`.
2. **Tool validation.** Every write handler validates its arguments (`agent/tools/_validation.py`)
   and checks a referenced transaction actually exists before touching `user.*`, turning a bad
   call into a structured `ToolInputError` result rather than a raw FK violation or an
   unvalidated write.

A merchant name, transaction note, or tag is untrusted text that reached the database from
outside — the agent's tools only ever store it or search it, never execute it. See
`tests/integration/test_agent_tools.py::test_add_transaction_note_stores_arbitrary_text_verbatim`.

## Auditing

Every tool call — read or write, successful or not — is written to `agent.tool_calls`
(`agent/loop.py::_audit_tool_call`) with the tool name, its (already-validated) arguments, a
structured `result_summary`, whether it was a write, and **which provider served the call**. An
unknown tool name or a failed validation is audited the same way, as a structured error, rather
than skipped — the audit trail is a record of everything the model *attempted*, not just what
succeeded.

## Conversation persistence

Handoff §8.3: persist only what is useful. `agent.conversations`/`agent.messages`
(`agent/conversation.py`) hold just the user's text and the assistant's final answer per turn.
The tool-call/tool-result round trips within a turn are ephemeral — already captured, more
usefully, in the `agent.tool_calls` audit trail — and are not replayed as conversation history on
the next turn; `load_history` reconstructs only the durable `user`/`assistant` exchanges.

## Tool-call budget

`agent/loop.py::MAX_TOOL_CALLS_PER_TURN` (8) bounds both a non-converging model and the blast
radius of a prompt-injection attempt trying to chain many write-tool calls from one request. On
exhaustion the loop returns a fixed fallback message rather than looping forever or crashing.

## Adding a new tool

1. Put the arithmetic in `analytics/` if it isn't there already — a tool handler wraps a
   deterministic function, it never computes a figure itself.
2. Add a `ToolDefinition` in `agent/tools/read.py` or `write.py` with a JSON Schema for its
   inputs; validate with the helpers in `_validation.py`.
3. Write tools only ever touch `user.*`/`finance.*` — never add a handler that reaches `plaid.*`.
4. Add it to `READ_TOOLS`/`WRITE_TOOLS` — both provider adapters pick it up automatically.
5. Add an integration test in `tests/integration/test_agent_tools.py` (happy path + at least one
   validation failure) and, if it's a write tool, confirm the audit row in a loop-level test.
