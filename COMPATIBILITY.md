# Compatibility

This release targets local Linux usage with Python 3.11+. Windows is unsupported because authentication uses POSIX file locks. macOS has not been validated. No automatic systemd installation is included.

The underlying bridge was exercised with Claude Code 2.1.291. The release suite tests the translated HTTP/SSE protocol with synthetic providers. A mocked protocol test is not a guarantee that every model, Claude release, account, or OAuth deployment works.

| Feature | Behavior |
| --- | --- |
| ChatGPT plan access | Official user-consented SIWC, public `api.openai.com/v1`; account eligibility required |
| Inference | Responses streaming with `store:false`, complete required history sent on each call |
| Function tools | Translated calls and results; executed by Claude Code |
| Local MCP tools | Usable when Claude exposes them as client-side function tools; hosted Responses MCP is unsupported |
| Deferred Claude tools | Local ToolSearch references become `additional_tools` input records |
| Web search | Translated only when supported by the selected model and account policy |
| Images and PDF/text documents | Translated inline; selected model must support them |
| Audio/video and Files upload API | Unsupported |
| Provider compaction blocks | Rejected; use a visible conversation summary |
| Native computer use, hosted Code Interpreter/image generation | Unsupported |
| `stop_sequences` | Rejected rather than silently removed; use the manual approval launcher |
| Anthropic `max_tokens` | Not enforced upstream; SIWC rejects `max_output_tokens` |
| Custom temperature/top-p | Not forwarded; model/provider defaults apply |
| Cache controls | Provider-default caching; no explicit TTL or guaranteed hit rate |
| Native Claude forwarding | Explicit opt-in; requires separate Claude OAuth, no subscription fallback |

When the client supplies request-class headers, small auxiliary work can use an available lighter ChatGPT model. Compaction may also use it when the account-reported context window is sufficient; recent observed prefix reuse can keep compaction on the main model. These decisions preserve all supplied input. Missing request-class headers disable this classification. Model availability comes from the signed-in account, not a bundled personal catalog.

Quota recovery retries only before output or tool activity. It honors provider `Retry-After` and a five-minute minimum between checks. Pending state does not survive client disconnection or a bridge restart; reconnect the request to resume waiting. Authentication errors and other failures are surfaced without switching provider.

Account changes require stopping and restarting `serve`: already-running requests cannot be recalled by changing local account selection. Use separate state directories and bridge ports for separate simultaneous accounts.

The [official preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) can change. Do not infer unlimited usage or compatibility from model names.
