import asyncio
import base64
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from claude_chatgpt_bridge import auth


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = {**jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key(), as_dict=True), 'kid': 'test-key'}

    def token(self, **overrides):
        claims = {'iss': auth.ISSUER, 'aud': 'synthetic-client', 'sub': 'synthetic-subject',
                  'exp': int(time.time()) + 300, 'nonce': 'synthetic-nonce', **overrides}
        return jwt.encode(claims, self.key, algorithm='RS256', headers={'kid': 'test-key'})

    def session(self):
        keys = self.jwk
        class Response:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def raise_for_status(self): pass
            async def json(self): return {'keys': [keys]}
        class Session:
            def get(self, url, **kwargs): return Response()
        return Session()

    async def test_valid_identity(self):
        value = await auth.verify_identity(self.session(), self.token(), 'synthetic-client', 'synthetic-nonce')
        self.assertEqual(value['sub'], 'synthetic-subject')

    async def test_rejects_wrong_issuer_audience_expiry_nonce_signature(self):
        for token, nonce in (
            (self.token(iss='https://example.invalid'), 'synthetic-nonce'),
            (self.token(aud='different-client'), 'synthetic-nonce'),
            (self.token(exp=1), 'synthetic-nonce'),
            (self.token(), 'wrong-nonce'),
            (jwt.encode({'sub': 'synthetic'}, 'synthetic-hmac-key' * 3, algorithm='HS256',
                        headers={'kid': 'test-key'}), 'synthetic-nonce'),
        ):
            with self.assertRaises((RuntimeError, jwt.InvalidTokenError)):
                await auth.verify_identity(self.session(), token, 'synthetic-client', nonce)

    async def test_refresh_cannot_change_account_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = auth.Auth(tmp)
            original = {'active': 'synthetic-client', 'profiles': {'synthetic-client': {
                'client_id': 'synthetic-client', 'access_token': 'synthetic-old-token',
                'refresh_token': 'synthetic-refresh', 'subject': 'original-subject',
                'scopes': ['chatgpt.tokens.use.direct'], 'expires_at': 0}}}
            auth.atomic_json(provider.path, original)
            response = {'access_token': 'synthetic-new-token', 'expires_in': 300,
                        'id_token': self.token(), 'token_type': 'bearer'}
            with patch.object(auth, 'token_request', AsyncMock(return_value=response)):
                with self.assertRaises(RuntimeError):
                    await provider.headers(self.session())
            self.assertEqual(provider.read(), original)

    async def test_missing_scope_prevents_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = auth.Auth(tmp)
            auth.atomic_json(provider.path, {'active': 'synthetic', 'profiles': {
                'synthetic': {'access_token': 'synthetic', 'scopes': []}}})
            with self.assertRaises(RuntimeError):
                await provider.headers(self.session())


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, granted):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            opened = asyncio.Event()
            exchanged = {}

            async def token_request(session, payload):
                exchanged.update(payload)
                return {'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh',
                        'id_token': 'synthetic-id-token', 'expires_in': 300, 'token_type': 'bearer',
                        'scope': 'openid chatgpt.tokens.use.direct' if granted else 'openid'}

            with patch.object(auth.webbrowser, 'open', side_effect=lambda url: opened.set()), \
                 patch.object(auth, 'token_request', token_request), \
                 patch.object(auth, 'verify_identity', AsyncMock(return_value={'sub': 'synthetic-subject'})):
                task = asyncio.create_task(auth.login(directory, port))
                try:
                    await asyncio.wait_for(opened.wait(), 5)
                    async with aiohttp.ClientSession() as session:
                        base = f'http://127.0.0.1:{port}'
                        async with session.get(base + '/authorize', allow_redirects=False) as response:
                            params = parse_qs(urlsplit(response.headers['Location']).query)
                        self.assertEqual(params['code_challenge_method'], ['S256'])
                        async with session.get(base + '/auth/callback', params={'state': 'incorrect'}) as response:
                            self.assertEqual(response.status, 400)
                        async with session.get(base + '/', headers={'Host': 'example.invalid'}) as response:
                            self.assertEqual(response.status, 403)
                        callback = {'state': params['state'][0], 'code': 'synthetic-code',
                                    'client_id': 'synthetic-issued-client'}
                        async with session.get(base + '/auth/callback', params=callback) as response:
                            self.assertEqual(response.status, 200 if granted else 400)
                        async with session.get(base + '/auth/callback', params=callback) as response:
                            self.assertEqual(response.status, 400)
                    expected = base64.urlsafe_b64encode(hashlib.sha256(exchanged['code_verifier'].encode()).digest()).rstrip(b'=').decode()
                    self.assertEqual(params['code_challenge'], [expected])
                    # Inspect state before awaiting the coroutine's exit status.
                    names = {p.name for p in directory.iterdir()}
                    self.assertNotIn('pending-exchange.json', names)
                    self.assertNotIn('unvalidated-login.json', names)
                    self.assertEqual((directory / 'chatgpt-auth.json').exists(), granted)
                    if granted:
                        await task
                    else:
                        # login reports failure as a value; CLI owns exit status.
                        self.assertFalse(await task)
                finally:
                    if not task.done():
                        task.cancel()
                        try: await task
                        except asyncio.CancelledError: pass

    async def test_login_validates_state_and_keeps_only_validated_tokens(self):
        await self.exercise(True)

    async def test_denied_plan_scope_never_activates_account(self):
        await self.exercise(False)


class RefreshCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, succeeds):
        with tempfile.TemporaryDirectory() as tmp:
            provider = auth.Auth(tmp)
            original = {'active': 'synthetic-client', 'profiles': {'synthetic-client': {
                'client_id': 'synthetic-client', 'access_token': 'synthetic-old-access',
                'refresh_token': 'synthetic-old-refresh', 'subject': 'synthetic-subject',
                'scopes': ['chatgpt.tokens.use.direct'], 'expires_at': 0}}}
            auth.atomic_json(provider.path, original)
            loop = asyncio.get_running_loop()
            started = asyncio.Event()
            release = threading.Event()
            inference_started = asyncio.Event()

            class Response:
                status_code = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def iter_content(self, size):
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(5):
                        raise RuntimeError('Synthetic worker was not released')
                    if not succeeds:
                        raise RuntimeError('Synthetic refresh failure')
                    yield json.dumps({'access_token': 'synthetic-new-access',
                        'refresh_token': 'synthetic-new-refresh', 'expires_in': 3600,
                        'token_type': 'Bearer'}).encode()

            async def model_request():
                await provider.headers(None)
                inference_started.set()

            with patch.object(auth.requests, 'post', return_value=Response()) as exchange:
                task = asyncio.create_task(model_request())
                contender = None
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()  # A second disconnect/shutdown cancellation must not drop the lock.
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertTrue(provider.lock.locked())
                    with self.assertRaises(auth.Timeout):
                        with auth.FileLock(str(Path(tmp) / 'auth.lock'), timeout=0, mode=0o600):
                            pass
                    if succeeds:
                        # A different Auth instance must use the replacement, not race the worker.
                        contender = asyncio.create_task(auth.Auth(tmp).headers(None))
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 3)
                    self.assertFalse(inference_started.is_set())
                    if succeeds:
                        headers = await asyncio.wait_for(contender, 3)
                        self.assertEqual(headers['Authorization'], 'Bearer synthetic-new-access')
                        saved = provider.read()['profiles']['synthetic-client']
                        self.assertEqual(saved['refresh_token'], 'synthetic-new-refresh')
                        self.assertGreater(saved['expires_at'], time.time() + 3000)
                        self.assertEqual(exchange.call_count, 1)
                    else:
                        self.assertEqual(provider.read(), original)
                    self.assertFalse(provider.lock.locked())
                    with auth.FileLock(str(Path(tmp) / 'auth.lock'), timeout=0, mode=0o600):
                        pass
                finally:
                    release.set()
                    await asyncio.gather(task, *([contender] if contender else []), return_exceptions=True)

    async def test_cancelled_refresh_saves_replacement_and_serializes_next_caller(self):
        await self.exercise(True)

    async def test_refresh_failure_after_cancellation_preserves_credentials_and_releases_lock(self):
        await self.exercise(False)
