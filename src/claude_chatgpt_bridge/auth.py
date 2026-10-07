"""Official Sign in with ChatGPT OAuth, kept separate from Codex and Claude auth."""
import asyncio
import base64
import contextlib
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlencode
import uuid
import webbrowser

import aiohttp
from aiohttp import web
import jwt
import requests

from .state import default_directory, ensure_private_directory

ISSUER = 'https://auth.openai.com'
AUTHORIZE = ISSUER + '/api/accounts/authorize'
TOKEN = ISSUER + '/api/accounts/oauth/token'
RESOURCE = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
DEFAULT_DIR = default_directory()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Auth:
    def __init__(self, directory=DEFAULT_DIR):
        self.directory = ensure_private_directory(directory)
        self.path = self.directory / 'chatgpt-auth.json'
        self.lock = asyncio.Lock()

    def read(self):
        if not self.path.exists():
            return {'profiles': {}, 'active': None}
        return json.loads(self.path.read_text())

    def selected(self):
        data = self.read()
        profile = data['profiles'].get(data.get('active'))
        if not profile or not profile.get('access_token'):
            raise RuntimeError('ChatGPT is not connected. Run claude-chatgpt login.')
        if 'chatgpt.tokens.use.direct' not in profile.get('scopes', []):
            raise RuntimeError('Enable ChatGPT plan usage for this adapter, then sign in again.')
        return data, profile

    async def headers(self, session):
        async with self.lock:
            # There is only one bridge service; this file lock also protects login/refresh.
            async with file_lock(self.directory / 'auth.lock'):
                data, profile = self.selected()
                if profile.get('expires_at', 0) < time.time() + 120:
                    if not profile.get('refresh_token'):
                        raise RuntimeError('ChatGPT login expired. Run claude-chatgpt login.')
                    tokens = await token_request(session, {
                        'grant_type': 'refresh_token', 'client_id': profile['client_id'],
                        'refresh_token': profile['refresh_token'], 'resource': RESOURCE})
                    updated = merge_tokens(profile, tokens)
                    if 'id_token' in tokens:
                        claims = await verify_identity(session, tokens['id_token'], profile['client_id'])
                        if claims['sub'] != profile['subject']:
                            raise RuntimeError('Refreshed ChatGPT identity did not match; sign in again.')
                    data['profiles'][profile['client_id']] = updated
                    atomic_json(self.path, data)
                    profile = updated
                if 'chatgpt.tokens.use.direct' not in profile['scopes']:
                    raise RuntimeError('ChatGPT plan usage permission is no longer enabled.')
                return {'Authorization': 'Bearer ' + profile['access_token'],
                        'Content-Type': 'application/json', 'Accept': 'text/event-stream, application/json',
                        'Accept-Encoding': 'identity'}


@contextlib.asynccontextmanager
async def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        for _ in range(600):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError('ChatGPT authentication is busy; retry shortly.')
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


async def token_request(session, payload):
    def exchange():
        with requests.post(TOKEN, data=payload, headers={'Accept-Encoding': 'identity',
                           'Accept': 'application/json'}, allow_redirects=False,
                           timeout=(15, 30), stream=True) as response:
            if response.status_code != 200:
                raise RuntimeError(f'ChatGPT token exchange failed (HTTP {response.status_code}). Sign in again.')
            # Request uncompressed JSON for decoder compatibility. Keep strict
            # HTTP framing and all cryptographic validation.
            raw = bytearray()
            for chunk in response.iter_content(16384):
                raw.extend(chunk)
                if len(raw) > 256 * 1024:
                    raise RuntimeError('OpenAI returned an oversized OAuth response.')
            value = json.loads(raw)
            if not value.get('access_token') or value.get('token_type', '').lower() != 'bearer':
                raise RuntimeError('OpenAI returned an incomplete OAuth token response.')
            return value
    return await asyncio.to_thread(exchange)


def merge_tokens(profile, tokens):
    result = dict(profile)
    for key in ('access_token', 'refresh_token', 'id_token', 'token_type'):
        if key in tokens:
            result[key] = tokens[key]
    if 'scope' in tokens:
        result['scopes'] = tokens['scope'].split()
    result['expires_at'] = time.time() + int(tokens['expires_in'])
    result['saved_at'] = time.time()
    return result


async def verify_identity(session, token, client_id, nonce=None):
    async with session.get(ISSUER + '/.well-known/jwks.json', allow_redirects=False,
                           headers={'Accept-Encoding': 'identity'},
                           timeout=aiohttp.ClientTimeout(total=20)) as response:
        response.raise_for_status(); keys = await response.json()
    header = jwt.get_unverified_header(token)
    key = next((k for k in keys['keys'] if k['kid'] == header.get('kid')), None)
    if not key or header.get('alg') != 'RS256':
        raise RuntimeError('ChatGPT returned an unrecognized signing key.')
    claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=['RS256'],
                        audience=client_id, issuer=ISSUER,
                        options={'require': ['exp', 'iss', 'aud', 'sub']})
    if nonce is not None and not secrets.compare_digest(str(claims.get('nonce', '')), nonce):
        raise RuntimeError('ChatGPT sign-in nonce did not match.')
    return claims


async def login(directory, port, new_profile=False, open_browser=True):
    auth = Auth(directory)
    host_path = Path(directory) / 'host.json'
    if not host_path.exists():
        atomic_json(host_path, {'ext_agent_host_id': 'urn:uuid:' + str(uuid.uuid4())})
    host_id = json.loads(host_path.read_text())['ext_agent_host_id']
    data = auth.read()
    previous = None if new_profile else data['profiles'].get(data.get('active'))
    pending_path = Path(directory) / 'pending-registration.json'
    if previous is None and not new_profile and pending_path.exists():
        previous = json.loads(pending_path.read_text())
    client_id = previous['client_id'] if previous else 'dynamic_agent_client'
    state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
    redirect = f'http://127.0.0.1:{port}/auth/callback'
    params = {'client_id': client_id, 'ext_agent_host_id': host_id,
              'response_type': 'code', 'redirect_uri': redirect,
              'scope': SCOPES, 'resource': RESOURCE, 'state': state, 'nonce': nonce,
              'code_challenge_method': 'S256',
              'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()}
    if previous and previous.get('id_token'):
        params['id_token_hint'] = previous['id_token']
    if not previous:
        params['agent_name_hint'] = 'Claude ChatGPT Bridge'
    url = AUTHORIZE + '?' + urlencode(params)
    finished = asyncio.Event()
    result = {'ok': False}
    used = False
    session = aiohttp.ClientSession(headers={'Accept-Encoding': 'identity'})

    async def start(request):
        return web.Response(text='''<!doctype html><meta charset="utf-8"><title>Connect ChatGPT</title>
<style>body{font:18px system-ui;background:#171918;color:#eee;max-width:620px;margin:10vh auto;line-height:1.6}a{display:inline-block;background:#eee;color:#111;padding:12px 22px;border-radius:10px;text-decoration:none}</style>
<h1>Connect ChatGPT to Claude Code</h1><p>This local adapter lets you choose ChatGPT models in Claude Code. OpenAI will ask you to authorize use of your ChatGPT plan. This shares your existing plan allowance.</p><p>Your Claude subscription and existing chats stay in place.</p><a href="/authorize">Continue with ChatGPT</a>''', content_type='text/html')

    async def authorize(request):
        raise web.HTTPFound(url)

    async def callback(request):
        nonlocal used
        if not secrets.compare_digest(request.query.get('state', ''), state) or used:
            return web.Response(status=400, text='Invalid or already used sign-in state.')
        used = True
        try:
            if request.query.get('error'):
                raise RuntimeError('ChatGPT authorization was declined or could not be completed.')
            issued = request.query.get('client_id') or (client_id if previous else None)
            if not issued or issued == 'dynamic_agent_client' or (previous and issued != client_id):
                raise RuntimeError('ChatGPT client registration did not match this sign-in attempt.')
            # Preserve the issued registration even if the one-use code later expires.
            pending = {'client_id': issued, 'ext_agent_host_id': host_id}
            atomic_json(Path(directory) / 'pending-registration.json', pending)
            exchange = {'grant_type': 'authorization_code',
                'client_id': issued, 'code': request.query['code'], 'code_verifier': verifier,
                'redirect_uri': redirect, 'resource': RESOURCE}
            tokens = await token_request(session, exchange)
            claims = await verify_identity(session, tokens['id_token'], issued, nonce)
            if previous and previous.get('subject') and claims['sub'] != previous['subject']:
                raise RuntimeError('The signed-in account differs from the selected account.')
            profile = merge_tokens({'client_id': issued, 'ext_agent_host_id': host_id,
                'issuer': ISSUER, 'subject': claims['sub'], 'email': claims.get('email', ''),
                'scopes': []}, tokens)
            if 'chatgpt.tokens.use.direct' not in profile['scopes']:
                raise RuntimeError('ChatGPT plan usage was not granted. Enable it in ChatGPT and sign in again.')
            async with file_lock(Path(directory) / 'auth.lock'):
                current = auth.read(); current['profiles'][issued] = profile; current['active'] = issued
                atomic_json(auth.path, current)
            result['ok'] = True
            message = 'ChatGPT connected. You can close this tab; the adapter is ready for testing.'
            status = 200
        except Exception as error:
            result['error'] = str(error) if isinstance(error, RuntimeError) else 'ChatGPT sign-in validation failed; please retry.'
            message, status = result['error'], 400
        finally:
            finished.set()
        return web.Response(status=status, text=html.escape(message), content_type='text/html')

    @web.middleware
    async def local_browser_only(request, handler):
        if request.host != f'127.0.0.1:{port}' or request.headers.get('Origin'):
            return web.Response(status=403, text='Use the local sign-in address.')
        try:
            response = await handler(request)
        except web.HTTPException as redirect_response:
            response = redirect_response
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    app = web.Application(middlewares=[local_browser_only])
    app.router.add_get('/', start); app.router.add_get('/authorize', authorize)
    app.router.add_get('/auth/callback', callback)
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup(); await web.TCPSite(runner, '127.0.0.1', port).start()
        print(f'Continue with ChatGPT: http://127.0.0.1:{port}/', flush=True)
        if open_browser:
            webbrowser.open(f'http://127.0.0.1:{port}/')
        await asyncio.wait_for(finished.wait(), 1800)
        print(json.dumps(result), flush=True)
        await asyncio.sleep(1)
    finally:
        await runner.cleanup(); await session.close()
    return result['ok']
