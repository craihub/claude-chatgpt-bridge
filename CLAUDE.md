# Install Claude ChatGPT Bridge for the desktop app

The user giving you this repository already has a working Claude Code desktop agent. When they ask you to install it, perform the installation. The user should complete ChatGPT sign-in and consent personally. Do not ask them to type shell commands or edit configuration files you can handle.

## Scope and facts

- Target the **Code tab of Claude Desktop**, not just the terminal CLI. Do not call the task complete after testing `claude` in a terminal.
- Native CI exercises Linux, macOS and Windows protocol/storage checks and release builds. Check its latest result. Desktop support remains provisional until the native service lifecycle and real desktop onboarding pass on the user's platform; a generated service file does not prove either.
- Use the supported desktop **Import configuration** UI. Do not patch application bundles, ASAR files, registry policy, managed preferences, or invent undocumented config-library filenames.
- This creates a separate named gateway configuration, which changes desktop inference when selected. It does not add ChatGPT models to an existing Bedrock/Vertex/native configuration in place. Keep the original configuration available. Existing chats and provider credentials must be preserved.
- The gateway flow may affect Chat and Cowork too; Code is the target and the new desktop onboarding flow still needs end-to-end validation. Do not advertise support for the other tabs or bypass errors for their unsupported tools.
- An organization-managed/read-only configuration takes precedence. Never bypass it. Report that the administrator must permit the gateway.

## Installation workflow

1. Inspect the user's OS and installed Claude Desktop version. Confirm the desktop app exposes third-party inference configuration and that local changes are allowed. Check Python (`python3` on Linux/Mac, `py -3` on Windows); use 3.11+. If missing, install Python using the official platform installer/package manager within the user's authorization, honoring any required OS approval. Do not disable endpoint protections or install a second Claude client.

2. Keep this repository checkout for the guide and updates. Run `python3 install.py setup` (Windows: `py -3 install.py setup`). The script installs into an isolated per-user runtime outside the checkout, signs in, discovers the account's models, prepares private configuration, and starts a per-user background service. It may run for up to 30 minutes waiting for browser consent; keep its process alive while the user signs in. Never enter credentials for them, read token files, or echo callback URLs. If interrupted, repeat `install.py continue`. Existing identity and keys are reused.

3. Read only the redacted JSON result. Exit code **2 means setup is pending**, not a failed install. If phase is `awaiting_login`, repeat without `--no-login` and let the user authorize. Resolve service/dependency errors before touching desktop routing. `doctor` never makes model requests. To select a different account-listed model, rerun `setup --model chatgpt.EXACT_ID`; do not guess an ID.

   Setup defaults desktop tool search on through the documented `toolSearchEnabled` key. Respect a user's existing preference and administrator restrictions: use `setup --no-tool-search` when it must remain off. The preference is saved for `continue`; `--tool-search` explicitly re-enables it. Verify actual tool deferral separately before claiming token savings, particularly on older desktop versions. Never use a force override against managed policy.

4. The result includes `config_file`, `model`, and `verification_prompt`. `config_file` contains a private local gateway key. Pass its path to the app's file chooser; **do not print/read its contents into the conversation, clipboard, screenshots, issue trackers, or Git**. The generated configuration never contains an OpenAI access token.

5. Save a checkpoint in this chat before a restart: the repository path, the command `python3 install.py doctor`, and the next action. Keep the setup result available. Desktop restarts may interrupt the installing agent; they cannot be guaranteed to resume it. Do not kill the app or interrupt unrelated work automatically.

6. Use your available desktop UI tools to open Help → Troubleshooting → Enable Developer Mode, then Developer → Configure Third-Party Inference. If these controls differ, use the current official documentation linked in `DESKTOP_SETUP.md`. Do not assume `~/.claude/settings.json` controls desktop gateway routing. If tools cannot operate the native app, explain the exact remaining import step and return pending; do not claim full automation.

7. Record whether the previous mode was standard Anthropic sign-in or an existing named third-party configuration. If a configuration already exists, export it as JSON **directly to private local storage**, keep its named profile, and run `install.py continue --previous-config /absolute/private/export.json`. The installer saves an immutable private backup. Do not overwrite a managed or existing provider profile. Create a new configuration named **Claude ChatGPT Bridge**, import `config_file` using the app's supported file import, and check that the provider is Gateway, the endpoint is `127.0.0.1` at the generated port, and the listed models match the setup result. Do not show the key field in screenshots. Do not broaden sandbox permissions or enable bypass mode.

8. Apply Changes / Save & Restart through the supported UI. Comply with the tool's approval requirements. If no tool approval is required and installation was authorized, no additional blanket confirmation is needed. The user may need to click an OS prompt or restart control. After restart, select the new third-party configuration and the `chatgpt.*` model. If the app refuses HTTP loopback or the model IDs, preserve the working original configuration and report the incompatibility; do not work around it by disabling security checks.

9. Send the exact `verification_prompt` from the setup result in a **new, empty Code conversation** using that model. This is the one small subscription inference request authorized by the installation task. It must use the desktop UI, with no attachments, repository history, MCP calls or tools. Avoid retrying a quota or auth failure repeatedly. Do not simulate this step with curl, a CLI, or the bridge key: it is meant to prove desktop routing.

10. Run `python3 install.py doctor` (Windows: `py -3 install.py doctor`). Success requires `desktop_verified: true`, a healthy bridge, and an installed background service. The bridge sets verification only after the desktop credential receives a completed response with the exact marker. The key is a routing check, not cryptographic proof of which UI sent it; you must also have observed the desktop result. Report the chosen model, the next-login startup behavior, and how to undo. If it remains pending, state the concrete remaining step.

    Describe this as verification of the short desktop routing check. It does not establish tool approvals, MCP discovery, images, compaction, cross-provider switching in one dropdown, or long quota waits. Test the intended workload separately within the user's authorization. Do not generate extra paid test requests solely to make the setup claim broader.

## Undo and upgrades

Run `install.py undo` first. It leaves the bridge running and tells you how to restore the previous desktop route. Select the old named profile or standard Anthropic sign-in and restart; if necessary, import the saved `desktop-previous.json` without printing it. Only after observing that restoration, run `install.py undo --desktop-restored`. That stops/removes the owned service and deletes the bridge's desktop-local key/config. It preserves OAuth state and the previous-config backup; remote revocation is done by the user in ChatGPT. Delete the now-unused named bridge profile in the desktop UI through its normal recoverable workflow if available.

For upgrades, update this checkout and rerun `install.py continue`. The runtime is rebuilt only when public source changes. If import contents/models changed, reimport into the named bridge configuration and restart. Bridge Python code changes invalidate the old routing verification even when the import is identical: send the new verification prompt through Desktop and run `doctor` again. Old verification records without a code fingerprint also need a fresh check. Never overwrite unrelated service definitions or delete another app's runtime to resolve a conflict.

## Privacy and constraints

Never commit state, exports, credentials, logs, account model catalogs, user paths or screenshots containing identity. Tests and examples use synthetic accounts only. Do not change Bedrock or other provider settings, install elevated system services, change permissions on unrelated directories, change default system Python, or publish files as part of installation. Normal Claude tool approvals stay enabled.
