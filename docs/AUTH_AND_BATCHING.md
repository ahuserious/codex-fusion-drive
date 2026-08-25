# Authentication and batching

## Authentication matrix

| Transport | Authentication owner | Billing surface | Provider async batch |
|---|---|---|---|
| xAI Responses | `XAI_API_KEY` environment reference | Metered API | Not for Grok 4.5 |
| OpenRouter chat/Fusion | `OPENROUTER_API_KEY` environment reference | Metered API | No configured general async completion batch |
| OpenAI Responses | `OPENAI_API_KEY` environment reference | Metered API | Supported for `/v1/responses` |
| Anthropic Messages | `ANTHROPIC_API_KEY` environment reference | Metered API | Message Batches supported |
| Grok CLI OAuth | Grok CLI/keychain | Subscription | No |
| Claude Code OAuth | Claude CLI/keychain | Subscription | No |
| Codex host | Codex host | Host/subscription managed | Host-owned subagent scheduling only |

CLI OAuth tokens are never repurposed for API requests. The plugin removes the
corresponding API-key environment variable from OAuth child processes so the
requested auth path is unambiguous.

## CLI isolation

- Prompt content is written to a mode-0600 temporary file and passed on stdin,
  not exposed as a command-line argument.
- Claude runs in safe mode with tools disabled and no session persistence.
- Grok runs with tools, subagents, web search, and memory disabled in an isolated
  temporary working directory.
- Each subscription principal has a process-wide file lock and concurrency one.
- A timeout has ambiguous outcome and is not retried automatically.
- Raw identities and credential material are not returned or persisted.

## Batch modes

`provider_async` is selected only when the configured provider and model support
it. Otherwise the planner returns `bounded_microbatch` and the exact reason.

Provider batch submission is a separate, explicit, billable action:

1. `batch_plan`
2. `batch_prepare`
3. User confirmation
4. `batch_submit`
5. `batch_status`

Bounded microbatch concurrency, shared prompt prefixes, and cache keys can
improve throughput or cache utilization. They are not represented as a
guaranteed provider discount.

