import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from claude_chatgpt_bridge import auth, cli, state
from claude_chatgpt_bridge.bridge import convert_request
import test_recovery as recovery
from test_recovery import PAYLOAD


class StateTests(unittest.TestCase):
    def test_private_setup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'private'
            state.initialize(path)
            key = (path / 'bridge.key').read_bytes()
            state.initialize(path)
            self.assertEqual((path / 'bridge.key').read_bytes(), key)
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((path / 'bridge.key').stat().st_mode), 0o600)

    def test_rejects_state_inside_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.git').mkdir()
            (Path(tmp) / '.git/HEAD').write_text('ref: refs/heads/main\n')
            with self.assertRaises(ValueError):
                state.initialize(Path(tmp) / 'state')
            self.assertFalse((Path(tmp) / 'state').exists())

    @unittest.skipIf(os.name == 'nt', 'Windows ACLs tested separately')
    def test_rejects_symlinks_and_open_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'private'
            target.mkdir(mode=0o700)
            link = Path(tmp) / 'link'
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                state.initialize(link)
            target.chmod(0o755)
            with self.assertRaises(ValueError):
                state.initialize(target)
            target.chmod(0o700)
            (target / 'bridge.key').symlink_to(Path(tmp) / 'outside')
            with self.assertRaises(ValueError):
                state.initialize(target)

    def test_xdg_and_override(self):
        with patch('claude_chatgpt_bridge.platforms.platform_name', return_value='linux'), \
             patch('claude_chatgpt_bridge.platforms.Path.home', return_value=Path('/tmp/synthetic-home')), \
             patch.dict(os.environ, {'XDG_STATE_HOME': '/tmp/synthetic-state'}, clear=True):
            self.assertEqual(state.default_directory(), Path('/tmp/synthetic-state/claude-chatgpt-bridge'))
        with patch.dict(os.environ, {'CLAUDE_CHATGPT_STATE_DIR': '/tmp/synthetic-override'}):
            self.assertEqual(state.default_directory(), Path('/tmp/synthetic-override'))

    def test_atomic_credentials_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'synthetic.json'
            auth.atomic_json(path, {'active': None})
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_launch_environment_does_not_modify_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = state.initialize(tmp)
            original = {'ANTHROPIC_AUTH_TOKEN': 'synthetic-other-provider',
                        'ANTHROPIC_BASE_URL': 'https://example.invalid',
                        'ANTHROPIC_CUSTOM_HEADERS': 'x-test: synthetic-sensitive',
                        'CLAUDE_CODE_USE_BEDROCK': '1', 'PATH': '/usr/bin'}
            expected = dict(original)
            result = cli.launch_environment(path, 12345, original)
            self.assertEqual(original, expected)
            self.assertNotIn('ANTHROPIC_AUTH_TOKEN', result)
            self.assertNotIn('CLAUDE_CODE_USE_BEDROCK', result)
            self.assertEqual(result['ANTHROPIC_BASE_URL'], 'http://127.0.0.1:12345')
            self.assertNotIn('synthetic-sensitive', json.dumps(result))

    def test_status_omits_identity_and_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = state.initialize(tmp)
            auth.atomic_json(path / 'chatgpt-auth.json', {'active': 'synthetic-account', 'profiles': {
                'synthetic-account': {'email': 'sample@example.invalid', 'access_token': 'synthetic-token',
                                      'scopes': ['chatgpt.tokens.use.direct']}}})
            result = json.dumps(cli.account_status(path))
            self.assertNotIn('sample@', result)
            self.assertNotIn('synthetic-token', result)
            self.assertNotIn('synthetic-account', result)
            self.assertTrue(cli.account_status(path)['connected'])


class EndpointPrivacyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown
    send = recovery.RecoveryTests.send
    async def test_authentication_required_for_health_and_api(self):
        for route in ('/health', '/v1/models'):
            reply = await self.client.get(route)
            self.assertEqual(reply.status, 401)
        reply = await self.client.post('/v1/messages', json=PAYLOAD)
        self.assertEqual(reply.status, 401)
        self.assertEqual(self.requests, [])

    async def test_browser_origin_rejected_even_with_key(self):
        reply = await self.client.post('/v1/messages', json=PAYLOAD,
            headers={**self.headers, 'Origin': 'https://example.invalid'})
        self.assertEqual(reply.status, 403)
        self.assertEqual(self.requests, [])

    async def test_upstream_never_receives_incoming_authentication(self):
        captured = {}
        from aiohttp import web
        from test_recovery import sse, created, delta, complete
        async def inspect(request):
            captured.update(request.headers)
            return await sse(request, [created(), delta(), complete()])
        self.actions.append(inspect)
        self.headers.update(Authorization='Bearer synthetic-claude-token',
                            **{'x-api-key': 'synthetic-anthropic-key'})
        await self.send()
        self.assertEqual(captured['Authorization'], 'Bearer fake-test-token')
        serialized = json.dumps(captured)
        for marker in ('synthetic-claude-token', 'synthetic-anthropic-key', 'synthetic-local-test-key'):
            self.assertNotIn(marker, serialized)

    async def test_audit_discards_arbitrary_provider_fields(self):
        self.bridge.audit('completed', code='synthetic-private-error', response='synthetic-response-id',
                          upstream_request_id='synthetic-request-id', server_model='synthetic-private-model',
                          requested_max_tokens={'prompt': 'synthetic-private-input'},
                          input_tokens='synthetic-private-count')
        log = (self.directory / 'usage-audit.jsonl').read_text()
        self.assertNotIn('synthetic-', log)


class ConversionTests(unittest.TestCase):
    def test_required_fields_and_unsupported_limits(self):
        result, _ = convert_request(PAYLOAD, 'gpt-6.1-sol')
        self.assertIs(result['store'], False)
        self.assertIs(result['stream'], True)
        self.assertNotIn('max_output_tokens', result)
        self.assertNotIn('previous_response_id', result)

    def test_tool_output_and_user_history_preserved(self):
        value = {**PAYLOAD, 'messages': [
            {'role': 'user', 'content': 'synthetic first prompt'},
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call_test',
                                             'name': 'example_tool', 'input': {'value': 1}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_test',
                                        'content': 'synthetic tool result'}]}]}
        result, _ = convert_request(value, 'gpt-6.1-sol')
        self.assertIn('synthetic first prompt', json.dumps(result))
        self.assertIn('synthetic tool result', json.dumps(result))
        self.assertEqual(result['input'][-1]['call_id'], 'call_test')
