"""Portable entry point. Credentials never appear in arguments or output."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys

import aiohttp

from .auth import Auth, atomic_json, file_lock, login
from .manage import refresh_models, usage_report
from .state import default_directory, ensure_private_directory, initialize


def launch_environment(directory, port, environment=None):
    original = os.environ if environment is None else environment
    env = {k: v for k, v in original.items()
           if not k.startswith('ANTHROPIC_') and k not in (
               'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
               'CLAUDE_CODE_USE_FOUNDRY')}
    key = (directory / 'bridge.key').read_text().strip()
    env.update(ANTHROPIC_BASE_URL=f'http://127.0.0.1:{port}',
               ANTHROPIC_API_KEY=key,
               ANTHROPIC_CUSTOM_HEADERS='x-local-claude-bridge-key: ' + key,
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    return env


async def health(directory, port):
    key = (directory / 'bridge.key').read_text().strip()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        async with session.get(f'http://127.0.0.1:{port}/health',
                               headers={'x-local-claude-bridge-key': key},
                               allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError('The local service did not accept this installation credential.')
            body = await response.json()
            if body.get('service') != 'claude-chatgpt-bridge':
                raise RuntimeError('The selected port is not a ChatGPT bridge.')
            return body


def account_status(directory):
    data = Auth(directory).read()
    profile = data.get('profiles', {}).get(data.get('active'), {})
    return {'connected': bool(profile.get('access_token')),
            'plan_usage_enabled': 'chatgpt.tokens.use.direct' in profile.get('scopes', []),
            'saved_accounts': len(data.get('profiles', {})),
            'quota_paused': (directory / 'quota-paused.json').exists()}


async def accounts(directory, select=None, sign_out=False, show_identity=False):
    auth = Auth(directory)
    async with file_lock(directory / 'auth.lock'):
        data = auth.read()
        keys = list(data.get('profiles', {}))
        if select is not None:
            if not 1 <= select <= len(keys):
                raise ValueError('Choose an account number from the accounts command.')
            data['active'] = keys[select - 1]
        if sign_out:
            data['active'] = None
        if select is not None or sign_out:
            atomic_json(auth.path, data)
        result = []
        for i, key in enumerate(keys, 1):
            item = {'account': i, 'active': key == data.get('active')}
            if show_identity:
                item['email'] = data['profiles'][key].get('email', '')
            result.append(item)
        return result


def parser():
    cli = argparse.ArgumentParser(description='Use your ChatGPT plan in a local Claude Code session.')
    cli.add_argument('--state-dir', type=Path, default=default_directory())
    cli.add_argument('--port', type=int, default=11438, help='Loopback bridge port (default 11438).')
    commands = cli.add_subparsers(dest='command', required=True)
    commands.add_parser('setup', help='Create private local state. Does not change Claude settings.')
    signin = commands.add_parser('login', help='Authorize your own ChatGPT account.')
    signin.add_argument('--callback-port', type=int, default=11439)
    signin.add_argument('--no-browser', action='store_true')
    signin.add_argument('--new-account', action='store_true')
    commands.add_parser('models', help='Refresh your account model list; no inference.')
    serve = commands.add_parser('serve', help='Run the bridge on 127.0.0.1; Ctrl+C to stop.')
    serve.add_argument('--allow-claude', action='store_true', help='Allow explicit Claude-model forwarding with separate Claude OAuth.')
    run = commands.add_parser('run', help='Launch Claude Code with settings limited to this process.')
    run.add_argument('--model', required=True, help='Exact chatgpt.* model shown by models.')
    run.add_argument('claude_args', nargs=argparse.REMAINDER)
    commands.add_parser('status', help='Show local connection status without identity or network access.')
    commands.add_parser('usage', help='Summarize local token counts; no network access.')
    commands.add_parser('resume', help='Clear the cooldown after a known reset; no inference.')
    account = commands.add_parser('accounts', help='List or choose saved accounts. Restart serve after switching.')
    account.add_argument('--select', type=int)
    account.add_argument('--show-identity', action='store_true', help='Print saved email addresses locally.')
    commands.add_parser('logout', help='Deselect active account; revoke access separately in ChatGPT.')
    return cli


def main():
    os.umask(0o077)
    args = parser().parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit('Choose a nonprivileged port from 1024 to 65535.')
    try:
        if args.command in ('setup', 'login'):
            directory = initialize(args.state_dir)
        else:
            if not args.state_dir.exists():
                raise ValueError('Run claude-chatgpt setup first.')
            directory = ensure_private_directory(args.state_dir)
        if args.command == 'setup':
            print('Private state ready. Next: claude-chatgpt login')
        elif args.command == 'login':
            if not 1024 <= args.callback_port <= 65535:
                raise ValueError('Choose a nonprivileged callback port.')
            if not asyncio.run(login(directory, args.callback_port, args.new_account, not args.no_browser)):
                raise SystemExit(1)
            print('Next: claude-chatgpt models, then claude-chatgpt serve')
        elif args.command == 'models':
            print(json.dumps(asyncio.run(refresh_models(directory)), indent=2))
            print('Restart serve to load this model list.', file=sys.stderr)
        elif args.command == 'serve':
            from aiohttp import web
            from .bridge import Bridge
            service = Bridge(directory, args.port, allow_claude=args.allow_claude)
            if not service.models:
                raise ValueError('No models saved. Run claude-chatgpt models first.')
            logging.basicConfig(level=logging.WARNING)
            print(f'Bridge listening on http://127.0.0.1:{args.port}; Ctrl+C to stop.', flush=True)
            web.run_app(service.app(), host='127.0.0.1', port=args.port,
                        handler_cancellation=True, access_log=None, print=None)
        elif args.command == 'run':
            executable = shutil.which('claude')
            if executable is None:
                raise ValueError('Install Claude Code separately and ensure claude is on PATH.')
            models = json.loads((directory / 'models.json').read_text())
            if args.model not in {'chatgpt.' + row['slug'] for row in models}:
                raise ValueError('Choose a model returned by claude-chatgpt models.')
            asyncio.run(health(directory, args.port))
            extra = args.claude_args
            if extra[:1] == ['--']:
                extra = extra[1:]
            # Keep tool approvals enabled. Do not modify persistent settings.
            command = [executable, '--permission-mode', 'manual', '--model', args.model, *extra]
            raise SystemExit(subprocess.call(command, env=launch_environment(directory, args.port)))
        elif args.command == 'status':
            print(json.dumps(account_status(directory), indent=2))
        elif args.command == 'usage':
            print(json.dumps(usage_report(directory), indent=2))
        elif args.command == 'resume':
            (directory / 'quota-paused.json').unlink(missing_ok=True)
            print('Local cooldown cleared. Your next pending request checks allowance; no request sent by this command.')
        elif args.command in ('accounts', 'logout'):
            result = asyncio.run(accounts(directory, getattr(args, 'select', None),
                                           args.command == 'logout', getattr(args, 'show_identity', False)))
            print(json.dumps(result, indent=2))
            if args.command == 'logout' or getattr(args, 'select', None) is not None:
                print('Stop and restart serve to end in-flight work and clear account-specific memory.', file=sys.stderr)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, aiohttp.ClientError, asyncio.TimeoutError, KeyError):
        print('Could not complete the command. Check setup, private state permissions and the local service. '
              'Run login again if authorization expired.', file=sys.stderr)
        raise SystemExit(1) from None
