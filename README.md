# Claude ChatGPT Bridge

Use your own ChatGPT plan in Claude Code through a local adapter for OpenAI's official **Sign in with ChatGPT** flow.

Experimental, Linux-first, and unofficial. This project is not affiliated with or endorsed by OpenAI or Anthropic. Install Claude Code separately; no proprietary client code or prompts are distributed here.

The bridge translates Anthropic Messages requests into Responses API requests, streams the results back to Claude Code, and keeps OAuth credentials on your machine. Claude Code executes your local tools with its normal permission controls.

## What it provides

- Browser sign-in, PKCE, ID-token validation, and access-token refresh.
- Account-specific model discovery, text, supported images/documents, and function tools.
- Streaming responses and preservation of tool-call history and opaque reasoning records.
- Stable conversation prefixes and local token/cache accounting. Cache hits and savings are not guaranteed.
- Automatic waiting after a subscription quota error, with five-minute minimum intervals and longer provider cooldowns respected. Checks stop when the waiting request disconnects. Requests that already produced output or tool activity are never automatically replayed.
- A process-scoped Claude launcher: setup does not edit your Claude settings, install a service, or alter other model providers.

Your plan's limits still apply. This is not a way to bypass allowance or account access restrictions. Eligibility, models, and supported features depend on the account and the preview API.

## Requirements

- Linux, Python 3.11 or newer, and a browser on the same computer.
- Claude Code installed with `claude` on your `PATH`.
- A ChatGPT account eligible for plan usage, with permission explicitly granted during sign-in.
- Free loopback ports 11438 (bridge) and 11439 (login).

The source integration was exercised with Claude Code 2.1.291. Other client versions may change the protocol. See [compatibility](COMPATIBILITY.md) before relying on a particular feature.

## Install and connect

Download or clone this repository, then run from its directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
claude-chatgpt setup
claude-chatgpt login
claude-chatgpt models
```

Follow **Continue with ChatGPT** in the local browser page. Each installation creates its own opaque host identifier; each account authorizes its own registration. No shared client credentials are included.

Start the bridge in that terminal:

```sh
claude-chatgpt serve
```

In another terminal, activate the same environment and choose an exact model identifier returned by `models`:

```sh
. .venv/bin/activate
claude-chatgpt run --model chatgpt.MODEL_FROM_YOUR_LIST
```

The placeholder must be replaced with an available model. You can pass Claude options after `--`, for example `-- --continue`. The launcher selects manual tool approvals; it does not disable safety checks.

`serve` stays in the foreground. Stop it with Ctrl+C. After refreshing the model list or switching accounts, restart it. Run `claude` normally for your usual provider configuration.

## State and privacy

The default directory is `$XDG_STATE_HOME/claude-chatgpt-bridge`, or `~/.local/state/claude-chatgpt-bridge` when XDG is unset. It contains OAuth credentials, a local bridge key, the account model list, quota state, and a small rotating usage ledger. These are **runtime data, never files to upload or commit**.

Directories must be owned by you and private (`0700`); files must be private (`0600`). Symlinked state and state inside Git checkouts are refused. Use a dedicated directory. Override it with `CLAUDE_CHATGPT_STATE_DIR` or `--state-dir` before the command. Set `--port` before the command on both `serve` and `run` when changing ports.

The bridge binds only to `127.0.0.1`, requires a local key for its API and health endpoint, and rejects browser-origin inference requests. Tokens are never command-line arguments. Only ChatGPT authorization is sent to OpenAI. Explicit Claude forwarding, disabled by default, requires its own Claude OAuth credential and `serve --allow-claude`.

Prompts, tool results, and attachments are sent to the selected provider to answer your requests. Claude Code may retain them in its own session history. The bridge keeps bounded continuation data in memory, but its usage ledger records counts, model IDs and keyed fingerprints rather than conversation bodies or tokens. There is no project telemetry or automatic log upload. See [security and data handling](SECURITY.md).

## Maintenance

```sh
claude-chatgpt status                # Offline, identity redacted
claude-chatgpt usage                 # Offline retained token totals
claude-chatgpt accounts              # Numbered saved registrations
claude-chatgpt accounts --show-identity  # Explicitly print saved emails locally
claude-chatgpt accounts --select 1    # Restart serve after switching
claude-chatgpt login --new-account
claude-chatgpt logout                # Deselect account; stop/restart serve
```

Logout deselects the account locally; it does not revoke saved refresh tokens. Revoke the app's access in ChatGPT to end the authorization. Keep the host identifier stable if you intend to reconnect the same installation.

Normally quota recovery is automatic while a request remains connected. If the client already displayed an error, retry it once to attach a waiting request. `claude-chatgpt resume` clears local cooldown without making inference; use it only after a known reset. Usage totals are not your account's billing statement or remaining allowance percentage.

## Remove

Stop `serve` and close the launched Claude session. Run `python -m pip uninstall claude-chatgpt-bridge` in the installation environment, or remove that dedicated virtual environment. No persistent Claude settings were installed. Optionally revoke access in ChatGPT and delete the dedicated private state directory after checking its path. Do not publish it in an issue report.

## Develop and validate

```sh
python -m pip install '.[dev]'
python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"
python -m pytest -q
python scripts/check_public.py
python scripts/build_release.py
```

Dependency installation and the initial tokenizer download use the network. Tests use synthetic credentials and loopback mock providers; they reject external network connections. They do not call models or consume subscription allowance. The release script builds from an allowlisted temporary copy, removes local user/group archive metadata, and scans both source and built packages. Dependency auditing uses package metadata, not account state:

```sh
python -m pip freeze --exclude claude-chatgpt-bridge > /tmp/bridge-dependencies.txt
python -m pip_audit -r /tmp/bridge-dependencies.txt --no-deps --disable-pip
```

## Official integration references

- [Sign in with ChatGPT for open-source apps](https://developers.openai.com/siwc/token-sharing-open-source)
- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)

MIT licensed. Contributions should use synthetic reproductions; see [CONTRIBUTING.md](CONTRIBUTING.md).
