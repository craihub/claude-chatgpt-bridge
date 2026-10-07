"""Resumable desktop onboarding; app changes use its supported import UI."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time

import aiohttp

from .auth import Auth, atomic_json, file_lock, login
from .manage import refresh_models
from .platforms import platform_name
from .state import initialize
from . import service

DOCS_URL = 'https://claude.com/docs/third-party/claude-desktop/in-app-configuration'


def read_record(directory):
    path = directory / 'desktop-setup.json'
    return json.loads(path.read_text()) if path.exists() else {}


def check_current(directory, check, record):
    """A saved response verifies only its original account/config and time window."""
    try:
        _, account = Auth(directory).selected()
        config = json.loads((directory / 'desktop-import.json').read_text())
        return (bool(check.get('marker')) and check.get('model') == record.get('model')
                and check.get('account') == hashlib.sha256(account['client_id'].encode()).hexdigest()
                and check.get('config') == hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
                and 0 <= time.time() - check.get('created_at', 0) <= 7 * 86400)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return False


def pick_port(preferred=11438):
    for port in [preferred, 0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(('127.0.0.1', port))
                return sock.getsockname()[1]
            except OSError:
                if port == 0:
                    raise


def make_config(models, key, port, selected):
    aliases = {'chatgpt.' + m['slug']: m for m in models}
    if selected not in aliases:
        raise ValueError('The selected model is not available for this account. Run models to list choices.')
    ordered = [selected, *(m for m in aliases if m != selected)]
    return {'inferenceProvider': 'gateway', 'inferenceCredentialKind': 'static',
            'inferenceGatewayBaseUrl': f'http://127.0.0.1:{port}',
            'inferenceGatewayApiKey': key, 'inferenceGatewayAuthScheme': 'bearer',
            'inferenceModels': [{'name': m, 'labelOverride': aliases[m]['display_name'] + ' · ChatGPT'}
                                for m in ordered],
            'defaultModelEffort': 'medium'}


async def probe(directory, port, *, desktop=False):
    key = (directory / ('desktop.key' if desktop else 'bridge.key')).read_text().strip()
    headers = {'Authorization': 'Bearer ' + key} if desktop else {'x-local-claude-bridge-key': key}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as client:
        async with client.get(f'http://127.0.0.1:{port}' + ('/v1/models' if desktop else '/health'),
                              headers=headers, allow_redirects=False) as reply:
            if reply.status != 200:
                return False
            body = await reply.json()
            if desktop:
                return isinstance(body.get('data'), list) and bool(body['data'])
            return body.get('service') == 'claude-chatgpt-bridge' and body.get('ok') is True


async def wait_ready(directory, port):
    for _ in range(30):
        try:
            if await probe(directory, port) and await probe(directory, port, desktop=True):
                return
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            pass
        await asyncio.sleep(.5)
    raise RuntimeError('Background service did not become healthy. Desktop configuration has not been applied. '
                       'Run desktop doctor, then repeat setup after fixing the service.')


def backup_previous(directory, source):
    """Save exact export bytes privately. Never print the configuration."""
    if not source:
        return None
    raw = Path(source).read_bytes()
    if len(raw) > 2 * 1024 * 1024 or not isinstance(json.loads(raw), dict):
        raise ValueError('Expected a JSON configuration exported by Claude Desktop.')
    target = directory / 'desktop-previous.json'
    if target.exists():
        if target.read_bytes() != raw:
            raise ValueError('A different original configuration is already backed up; it was preserved.')
    else:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
    return str(target)


async def setup(directory, *, model=None, no_login=False, previous_config=None, preferred_port=11438):
    directory = initialize(directory)
    async with file_lock(directory / 'desktop-install.lock'):
        record = read_record(directory)
        if record.get('undone'):
            record = {}
        backup = backup_previous(directory, previous_config)
        if backup:
            record['previous_config'] = backup
        if 'port' not in record:
            record['port'] = pick_port(preferred_port)
        record.setdefault('created_at', time.time())
        record.update(platform=platform_name(), phase='checking_login')
        atomic_json(directory / 'desktop-setup.json', record)
        try:
            Auth(directory).selected()
        except RuntimeError:
            if no_login:
                record['phase'] = 'awaiting_login'
                atomic_json(directory / 'desktop-setup.json', record)
                return {'phase': 'awaiting_login', 'next': 'Repeat setup without --no-login; the user signs in in the browser.'}
            callback_port = pick_port(11439)
            if not await login(directory, callback_port):
                record['phase'] = 'awaiting_login'
                atomic_json(directory / 'desktop-setup.json', record)
                raise RuntimeError('Sign-in did not grant plan access. Repeat setup to try again.')
        _, account = Auth(directory).selected()
        account_hash = hashlib.sha256(account['client_id'].encode()).hexdigest()
        old_models = (directory / 'models.json').read_bytes() if (directory / 'models.json').exists() else b''
        await refresh_models(directory)
        models = json.loads((directory / 'models.json').read_text())
        selected = model or record.get('model') or 'chatgpt.' + models[0]['slug']
        key_path = directory / 'desktop.key'
        if not key_path.exists():
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_hex(32) + '\n')
        key = key_path.read_text().strip()
        if len(key) < 32:
            raise ValueError('Desktop credential is invalid; do not apply its configuration.')
        config = make_config(models, key, record['port'], selected)
        atomic_json(directory / 'desktop-import.json', config)
        # Carry successful verification through a no-op resume only.
        check_path = directory / 'desktop-check.json'
        old_check = json.loads(check_path.read_text()) if check_path.exists() else {}
        config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if (old_check.get('model') != selected or not old_check.get('marker')
                or old_check.get('account') != account_hash
                or old_check.get('config') != config_hash
                or time.time() - old_check.get('created_at', 0) > 7 * 86400):
            atomic_json(check_path, {'marker': 'BRIDGE_SETUP_' + secrets.token_hex(12),
                'model': selected, 'account': account_hash, 'config': config_hash,
                'created_at': time.time(), 'completed': False})
        runtime_hash = hashlib.sha256(b''.join(p.read_bytes() for p in sorted(Path(__file__).parent.glob('*.py')))).hexdigest()
        restart = (old_models != (directory / 'models.json').read_bytes()
                   or record.get('account') != account_hash or record.get('runtime') != runtime_hash)
        record.update(account=account_hash, runtime=runtime_hash)
        record.update(model=selected, phase='starting_service')
        atomic_json(directory / 'desktop-setup.json', record)
        await asyncio.to_thread(service.install, directory, record['port'], restart=restart)
        await wait_ready(directory, record['port'])
        record['phase'] = 'awaiting_desktop_import'
        atomic_json(directory / 'desktop-setup.json', record)
        return await doctor(directory)


async def doctor(directory):
    record = read_record(directory)
    if not record:
        return {'phase': 'not_started', 'desktop_verified': False, 'next': 'Run desktop setup.'}
    if record.get('undone'):
        return {'phase': 'undone', 'desktop_verified': False}
    healthy = models_ok = False
    if (directory / 'bridge.key').exists():
        try:
            healthy = await probe(directory, record['port'])
            models_ok = healthy and await probe(directory, record['port'], desktop=True)
        except (OSError, aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass
    check_path = directory / 'desktop-check.json'
    check = json.loads(check_path.read_text()) if check_path.exists() else {}
    current = check_current(directory, check, record)
    confirmed = healthy and models_ok and current and check.get('completed') is True
    try:
        background = await asyncio.to_thread(service.status, directory)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        background = {'installed': False, 'running': False, 'conflict_or_error': True}
    confirmed = confirmed and background.get('installed') is True and background.get('running') is True
    result = {'phase': 'verified' if confirmed else record.get('phase', 'incomplete'),
              'platform': record.get('platform'), 'service': background,
              'bridge_healthy': bool(healthy), 'desktop_credential_accepted': bool(models_ok),
              'desktop_verified': bool(confirmed), 'model': record.get('model'),
              'config_file': str(directory / 'desktop-import.json'),
              'config_contains_local_secret': True, 'app_restart_required': not confirmed,
              'instructions': DOCS_URL}
    if current and not confirmed:
        result['verification_prompt'] = 'Reply exactly: ' + check['marker']
        result['next'] = ('In Claude Desktop, import the private JSON as a new named configuration, '
                          'apply and restart, select the listed ChatGPT model, and send verification_prompt '
                          'in a new empty Code conversation. Then run desktop doctor. '
                          'Do not claim success until desktop_verified is true.')
    elif not confirmed:
        result['next'] = 'Run desktop setup again to complete sign-in or renew verification for the current configuration.'
    return result


def observe_completion(directory, payload, model, response_content):
    """Called only for authenticated desktop-key requests that fully complete."""
    path = directory / 'desktop-check.json'
    if not path.exists():
        return
    check = json.loads(path.read_text())
    if (check.get('completed') or check.get('model') != model
            or not check_current(directory, check, read_record(directory))):
        return
    marker = check.get('marker', '')
    if not marker or time.time() - check.get('created_at', 0) > 7 * 86400:
        return
    last = next((m for m in reversed(payload.get('messages', [])) if m.get('role') == 'user'), {})
    def text(content):
        if isinstance(content, str):
            return content
        return ' '.join(b.get('text', '') for b in content or [] if b.get('type') == 'text')
    if marker not in text(last.get('content')) or text(response_content).strip() != marker:
        return
    check.update(completed=True, completed_at=time.time())
    atomic_json(path, check)


def undo(directory, desktop_restored=False):
    record = read_record(directory)
    if not record or record.get('undone'):
        return {'phase': 'undone', 'removed': False}
    if not desktop_restored:
        return {'phase': 'awaiting_desktop_restore',
            'previous_config_file': record.get('previous_config'),
            'next': 'In the app, select the previous configuration or standard Anthropic sign-in, '
                    'then restart. Only then run desktop undo --desktop-restored. '
                    'The bridge stays running until desktop routing is restored.'}
    result = service.uninstall(directory)
    # Revoke only the desktop-local bridge credential; OAuth stays private for reuse.
    for name in ('desktop.key', 'desktop-import.json', 'desktop-check.json'):
        (directory / name).unlink(missing_ok=True)
    record.update(undone=True, phase='undone')
    atomic_json(directory / 'desktop-setup.json', record)
    return {'phase': 'undone', **result, 'oauth_retained': True,
            'previous_config_retained': bool(record.get('previous_config'))}
