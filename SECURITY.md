# Security and data handling

This is experimental local software, not a security boundary against processes running as your user or an administrator. No scanner or test suite can promise zero risk.

## Credentials

Each user signs in through OpenAI's authorization page. The bridge validates OAuth state, PKCE, the signed ID token, issuer, audience, expiry, and nonce before activating a registration. Plan-usage scope is required. Token refresh preserves account identity. Credentials are not borrowed from installed Claude or Codex clients.

OAuth tokens, email/subject, registration IDs, host ID, and the local API key remain in a dedicated owner-only state directory. Tokens are stored as private files, not encrypted by an OS keychain. Full-disk encryption protects data at rest when the machine is locked down. Your user account can read these files. Protect local backups accordingly.

Only a registration ID and host ID are retained after an incomplete registration; authorization codes, PKCE verifiers, and unvalidated token responses are not saved for troubleshooting. The callback listener uses loopback and does not write HTTP access logs. Status output omits identities by default. The explicit `accounts --show-identity` command displays email addresses locally.

Desktop setup generates a distinct local gateway key. Its private import JSON contains that key, never OpenAI tokens. Import by file path without printing the contents. The desktop app necessarily stores its gateway configuration under its own security policy. Previous configuration exports may contain other providers' credentials: keep them in private local storage and never attach them to a chat or issue. The installer preserves an exact backup and refuses to overwrite it with different content.

The desktop key authorizes translated ChatGPT inference/model listing only; it cannot call admin health or native Claude forwarding. A random one-time marker records a completed setup response, bound to the account, configuration and a seven-day window. This is a routing check, not cryptographic proof of which UI sent it; the installation agent must also observe the response in Claude Desktop. `doctor` does not send inference requests.

Background service definitions contain paths, never credentials. The installer uses per-user startup and checks its receipt before replacing or removing a definition. Undo leaves the bridge running until the app's original route has been restored. Managed settings, application bundles and existing provider profiles must not be patched to force compatibility.

## Requests and logs

The service is loopback-only and key-authenticated. Browser-origin inference is rejected. OpenAI requests use their own OAuth credentials and fixed HTTPS endpoints with redirects disabled. Claude forwarding is opt-in and uses only the client's separate Claude credential. Unknown models fail; there is no fallback that bills another provider.

Usage logs are local and rotated to roughly three 2 MiB files. They contain timestamps, counts, model identifiers, routing decisions and keyed fingerprints. They do not intentionally contain prompts, attachments, tool bodies or credentials. Prefix evidence contains keyed hashes and counts. Raw conversation continuation is bounded in memory. Encrypted reasoning and replay metadata can be returned into Claude's ordinary local history.

Providers receive the context needed for inference; Claude's session history, tools, and their own logs are outside this project's storage policy. The project sends no diagnostics to its maintainers. Do not expose the service with a reverse proxy or bind it to a public interface.

## Publication checks

`PUBLIC_FILES.txt` is the explicit public-file allowlist. `scripts/check_public.py` checks tracked contents, all local Git objects including commit metadata, and optionally built archives for credential patterns, personal paths, email addresses outside reserved example domains, and unexpected files. It prints rule names and file identifiers, not matched values. The allowlist, `.gitignore`, tests and scanner are complementary safeguards, not proof that future changes are safe.

Build archives from clean reviewed source. Do not add runtime files even temporarily: deleting a secret in a later commit does not erase it from history. Review the diff and history before pushing. Use a GitHub no-reply identity if you do not want an email in future commits.

## Reporting

Never attach real credentials, OAuth callback URLs, account files, full logs or conversations to a public issue. Use a minimal synthetic reproduction. If private vulnerability reporting is enabled on the hosting repository, use its Security tab. Otherwise request a private reporting channel without publishing exploitation details or secrets. For any exposed credential, revoke it with the provider before attempting repository cleanup.
