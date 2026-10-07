# Compatibility

Python 3.11+ is required. The installer implements Linux user systemd, macOS LaunchAgents and Windows per-user scheduled tasks, with portable file locking and Windows state ACLs. Native CI exercises Python 3.11–3.13 on all three operating systems, including synthetic protocol tests, private storage, release builds and dependency audits; consult the latest workflow result. Native service lifecycle and real desktop installation tests remain pending, so desktop support is provisional on every platform.

The [desktop workflow](DESKTOP_SETUP.md) generates documented gateway import JSON and guides the installing agent through the app's supported UI. It requires an editable third-party configuration, local HTTP gateway acceptance, account-specific model IDs and a completed response in the actual Code tab. No desktop version has yet passed this new onboarding flow end to end. CLI protocol tests do not establish desktop compatibility. Installation remains pending when UI tools, permissions, a restart or provider compatibility prevent verification.

The underlying bridge was exercised with Claude Code 2.1.291. The release suite tests the translated HTTP/SSE protocol with synthetic providers. A mocked protocol test is not a guarantee that every model, Claude release, account, or OAuth deployment works.

| Feature | Behavior |
| --- | --- |
| ChatGPT plan access | Official user-consented SIWC, public `api.openai.com/v1`; account eligibility required |
| Inference | Responses streaming with `store:false`, complete required history sent on each call |
| Function tools | Translated calls and results; executed by Claude Code |
| Local MCP tools | Usable when Claude exposes them as client-side function tools; hosted Responses MCP is unsupported |
| Deferred Claude tools | Enabled by default in launcher/import; local ToolSearch references become `additional_tools` records; client support required |
| Web search | Translated only when supported by the selected model and account policy |
| Images and PDF/text documents | Translated inline; selected model must support them |
| Audio/video and Files upload API | Unsupported |
| Provider compaction blocks | Rejected; use a visible conversation summary |
| Native computer use, hosted Code Interpreter/image generation | Unsupported |
| `stop_sequences` | Rejected rather than silently removed; use the manual approval launcher |
| Anthropic `max_tokens` | Not enforced upstream; SIWC rejects `max_output_tokens` |
| Custom temperature/top-p | Not forwarded; model/provider defaults apply |
| Cache controls | Provider-default caching; no explicit TTL or guaranteed hit rate |
| Native Claude forwarding | CLI only, explicit opt-in; requires separate Claude OAuth, no subscription fallback |
| Desktop provider coexistence | Separate named configuration; preserves original profile for switching back, no mixed-provider patch |
| Desktop Chat/Cowork | Not validated; may share the selected gateway setting |

When the client supplies request-class headers, small auxiliary work can use an available lighter ChatGPT model. Compaction may also use it when the account-reported context window is sufficient; recent observed prefix reuse can keep compaction on the main model. These decisions preserve all supplied input. Missing request-class headers disable this classification. Model availability comes from the signed-in account, not a bundled personal catalog.

The terminal launcher defaults `ENABLE_TOOL_SEARCH=true` without replacing explicit environment preferences. Desktop imports set the documented `toolSearchEnabled` key; `setup --no-tool-search` saves an opt-out across resumes, and `--tool-search` re-enables it. Gateway versions bundling Claude Code 2.1.247 or later support the narrower tool-search request shape. Earlier desktop versions can enable additional experimental request features with this setting and need separate compatibility testing. Managed settings and tool denials take precedence; do not override them to force deferral. A generated setting or synthetic test is not proof that an installed desktop actually sends deferred tools. See [Claude Code tool search](https://code.claude.com/docs/en/mcp#configure-tool-search) and [desktop configuration](https://claude.com/docs/third-party/claude-desktop/configuration).

Quota recovery retries only before output or tool activity. It honors provider `Retry-After` and a five-minute minimum between checks. Pending state does not survive client disconnection, a client timeout or a bridge restart. Keep-alives do not guarantee a connection will remain open until a subscription window resets. Claude Desktop's documented gateway behavior allows roughly five minutes of keep-alives plus an additional idle wait (default 300 seconds; configurable from 300 to 1800). The generated configuration does not change that timeout. For longer cooldowns, let the client stop, wait for allowance, then retry once; the saved cooldown remains in force. See [gateway idle timeout](https://claude.com/docs/third-party/claude-desktop/gateway). Authentication errors and other failures are surfaced without switching provider.

Cancellation during access-token refresh waits for the already-started exchange, identity validation and credential save before releasing the auth locks. The cancelled model request does not resume inference. A process crash or forced termination can still interrupt this work; this protection applies to request cancellation, not abrupt process death.

Manual account changes require stopping and restarting `serve`: already-running requests cannot be recalled by changing local account selection. Desktop `install.py continue` refreshes models and restarts its owned service when the account or bridge Python code changes. Verification is bound to that code fingerprint, account and configuration; changes require a new desktop response. Older verification records without a code fingerprint are renewed once. Use separate state directories and bridge ports for separate simultaneous accounts.

The [official preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) can change. Do not infer unlimited usage or compatibility from model names.
