---
title: Smart Model Routing
description: Automatically escalate research, trading, and code-design gateway turns to a premium model.
sidebar_label: Smart Model Routing
sidebar_position: 8
---

# Smart Model Routing

Hermes can keep normal gateway conversation on a cost-efficient default model
and automatically escalate selected tasks to a premium model. Routing is local
and deterministic: it does not spend an additional LLM call to classify each
message.

## Configuration

```yaml
model:
  default: gpt-5.6-terra
  provider: openai-codex

smart_model_routing:
  enabled: true
  premium_model: gpt-5.6-sol
  premium_categories: research,trading,code_design
  premium_session_patterns: 'telegram:group:-1001234567890:(?:2|3)$'
  followup_turns: 2
  followup_ttl_seconds: 1800
```

`premium_categories` accepts either a comma-separated scalar or a YAML list.
`premium_session_patterns` accepts one regular expression or a YAML list of
regular expressions.

## Built-in categories

| Category | Typical triggers |
|---|---|
| `research` | Research, investigation, due diligence, literature review, fact-checking, or comparing evidence and primary sources |
| `trading` | Portfolios, brokers, orders, positions, equities, derivatives, execution, risk, slippage, and prediction markets |
| `code_design` | System/software architecture, API or schema design, concurrency models, component boundaries, and migration/refactor plans |

Simple code edits such as renaming a variable do not match `code_design`.
Transforming an existing research summary does not by itself match `research`.

## Session routing

`premium_session_patterns` is useful when an entire channel or topic has a
known purpose. Patterns match Hermes' gateway session key. For example, the
following keeps Telegram topics 2 and 3 on the premium model:

```yaml
premium_session_patterns:
  - 'telegram:group:-1001234567890:(?:2|3)$'
```

## Follow-up continuity

After a category match, short explicit continuation messages such as
`continue`, `go deeper on that`, or `implement that` remain on the same premium
model. `followup_turns` and `followup_ttl_seconds` bound this behavior. An
unrelated message immediately returns to the default model.

## Precedence and cache safety

- An explicit session `/model` selection always wins and disables automatic
  routing for that session.
- Routing changes the model only; it reuses the already-resolved provider and
  credentials. Configure the default and premium models on the same provider.
- The selected model participates in the gateway agent-cache signature. A model
  change rebuilds an incompatible cached agent rather than reusing the wrong
  transport.
- Routing errors fail closed to the default/session model so a malformed
  optional rule cannot block message delivery.

## Scope

Task-aware routing runs on gateway message turns. Cron jobs that need a
specific quality/cost tier should pin their own `model` and `provider`; auxiliary
tasks such as compression should use their dedicated `auxiliary.*` model
configuration.
