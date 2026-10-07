# Agent-assisted desktop setup

Give your existing Claude Code desktop agent this repository and ask:

> Install this bridge for the Code tab of my Claude Desktop app, following CLAUDE.md. Keep my existing provider configuration available. Handle installation and desktop configuration, let me sign into ChatGPT myself, and verify a real desktop response before calling it complete.

This flow automates the Python installation environment, OAuth handoff, account model discovery, local gateway configuration generation, background service, resumable status, and guarded undo. An agent with native desktop UI tools performs the app's import, restart, model selection and final test. Without those UI tools, the app import remains a manual step. Restarts and OS prompts may require a click. This is not a zero-click installer and it cannot guarantee that an agent interrupted by an app restart resumes itself.

## Commands for the installation agent

From the repository, use Python 3.11+:

| Action | Linux / macOS | Windows |
| --- | --- | --- |
| Install, sign in and prepare desktop | `python3 install.py setup` | `py -3 install.py setup` |
| Resume after interruption/update | `python3 install.py continue` | `py -3 install.py continue` |
| Read-only status | `python3 install.py doctor` | `py -3 install.py doctor` |
| Prepare rollback | `python3 install.py undo` | `py -3 install.py undo` |
| Finish rollback after restoring app | `python3 install.py undo --desktop-restored` | `py -3 install.py undo --desktop-restored` |

`setup --no-login` prepares the runtime without opening sign-in. `setup --model chatgpt.EXACT_ID` chooses a model from the authorized account; otherwise account catalog order determines the default. `--previous-config PATH` saves a private byte-for-byte backup of an existing app-exported JSON configuration. `--state-dir PATH` uses a dedicated private location outside Git. Repeat the same override on later commands.

Exit 0 means verified/undone, 2 means a next step is pending, and 1 means an error. `doctor` checks local HTTP endpoints and saved verification without sending inference. OAuth login and model discovery contact OpenAI. Verification uses one short prompt sent by the agent from a new empty desktop Code conversation; normal plan limits apply.

## Desktop import

The setup JSON points to `desktop-import.json`. This private file uses documented third-party configuration keys: gateway provider, static local key, bearer authentication, loopback URL, explicit account model IDs and medium default effort. It does not contain OpenAI tokens. Models are explicitly labeled ChatGPT and are not disguised as Claude models. HTTP loopback acceptance and custom model support must be verified on the installed app version.

Open **Help → Troubleshooting → Enable Developer Mode**, then **Developer → Configure Third-Party Inference**. Preserve the original named configuration or standard sign-in mode, create a separate **Claude ChatGPT Bridge** configuration, use **Import configuration** for the generated file, and apply/restart. A managed, read-only configuration must be handled by the administrator.

This gateway becomes the selected desktop inference configuration; it is not a patch that mixes providers into the existing configuration. Code is the supported target. Chat/Cowork may share the setting and are not validated by this project. Unsupported tools fail explicitly. Return to the previous configuration whenever needed.

Once the agent has sent `verification_prompt` from the desktop and observed the exact response, `doctor` must report `desktop_verified: true`. Success records only a random synthetic marker and timestamps, not conversation contents. A server health check alone is insufficient.

## Storage and startup

| Platform | Private state | Background startup |
| --- | --- | --- |
| Linux | XDG state directory / `~/.local/state/claude-chatgpt-bridge` | User systemd unit; requires a user systemd session |
| macOS | `~/Library/Application Support/ClaudeChatGPTBridge/state` | LaunchAgent at user login |
| Windows | `%LOCALAPPDATA%/ClaudeChatGPTBridge/state` | Current-user scheduled task at login; least privilege |

Runtime code lives separately in a per-user virtual environment outside the checkout. No system Python is changed. Linux/macOS use private POSIX permissions. Windows uses a protected directory ACL for the user and SYSTEM, checks ownership and allowed principals, and rejects links/reparse points. File locking uses a platform-aware library. Services contain executable paths and a state path, never token/key values.

The default port is 11438; setup picks an unused loopback port if occupied and saves it for later runs. Only this installation's service is managed. Existing files/tasks without its receipt are treated as conflicts. A crash can be repaired by repeating setup; no permanent success is recorded without verification.

## Validation status

Linux automated tests run locally. macOS and Windows branches have platform-specific CI jobs and synthetic checks, but real native desktop sign-in/import/restart testing is still required before declaring either supported. This repository is an experimental release candidate, not a universal installer guarantee.

## Official references

- [Desktop gateway configuration](https://claude.com/docs/third-party/claude-desktop/gateway)
- [In-app import and configuration](https://claude.com/docs/third-party/claude-desktop/in-app-configuration)
- [Configuration keys and model lists](https://claude.com/docs/third-party/claude-desktop/configuration)
- [Single-machine setup and restoring standard mode](https://claude.com/docs/third-party/claude-desktop/installation)
- [Sign in with ChatGPT](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
