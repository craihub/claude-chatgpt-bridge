"""Offline provider simulations through the real HTTP bridge and SSE parser."""
import asyncio
from collections import deque
from email.utils import formatdate
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from claude_chatgpt_bridge import bridge, quota_recovery
from claude_chatgpt_bridge.quota_recovery import QUOTA_CODE, ReplyStream, retry_delay
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

MODEL = 'gpt-6.1-sol'
PAYLOAD = {'model': 'chatgpt.' + MODEL, 'max_tokens': 100, 'stream': True,
           'messages': [{'role': 'user', 'content': 'PRIVATE TEST PROMPT'}]}


def quota():
    return web.json_response({'error': {'code': QUOTA_CODE, 'message': 'test limit'}},
                             status=429, headers={'Retry-After': '0.06'})


def created():
    return {'type': 'response.created', 'response': {'id': 'test-response', 'model': MODEL}}


def delta():
    return {'type': 'response.output_text.delta', 'output_index': 0,
            'content_index': 0, 'delta': 'Recovered.'}


def failed():
    return {'type': 'response.failed', 'response': {'error': {
        'code': QUOTA_CODE, 'message': 'test stream limit'}}}


def complete():
    return {'type': 'response.completed', 'response': {
        'id': 'test-response', 'model': MODEL,
        'output': [{'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': 'Recovered.'}]}],
        'usage': {'input_tokens': 10, 'output_tokens': 2}}}


async def sse(request, events):
    response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
    await response.prepare(request)
    for event in events:
        await response.write(('data: ' + json.dumps(event) + '\n\n').encode())
    await response.write_eof()
    return response


class FakeAuth:
    calls = 0

    async def headers(self, session):
        self.calls += 1
        return {'Authorization': 'Bearer fake-test-token'}


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        (self.directory / 'bridge.key').write_text('synthetic-local-test-key')
        (self.directory / 'models.json').write_text(json.dumps([
            {'slug': MODEL, 'display_name': 'Test model'}]))
        for path in self.directory.iterdir():
            path.chmod(0o600)
        self.actions, self.requests, self.times = deque(), [], []
        self.active = self.max_active = 0

        async def upstream(request):
            self.requests.append(await request.json())
            self.times.append(time.monotonic())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                action = self.actions.popleft() if self.actions else [created(), delta(), complete()]
                if callable(action):
                    return await action(request)
                if isinstance(action, web.Response):
                    return action
                return await sse(request, action)
            finally:
                self.active -= 1

        app = web.Application()
        app.router.add_post('/responses', upstream)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        self.url_patch = patch.object(bridge, 'RESOURCE', str(self.upstream.make_url('')).rstrip('/'))
        self.url_patch.start()
        self.reply_patch = patch.object(bridge, 'ReplyStream',
            lambda request, stream: ReplyStream(request, stream, heartbeat_seconds=.01))
        self.reply_patch.start()
        self.bridge = bridge.Bridge(self.directory, 0)
        self.bridge.auth = FakeAuth()
        self.bridge.quota.interval = .04
        self.client = TestClient(TestServer(self.bridge.app(), handler_cancellation=True))
        await self.client.start_server()
        self.headers = {bridge.KEY_HEADER: 'synthetic-local-test-key',
                        'x-claude-code-session-id': 'test-session',
                        'x-claude-code-request-class': 'main'}

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.url_patch.stop()
        self.reply_patch.stop()
        self.temp.cleanup()

    async def send(self, payload=None, path='/v1/messages'):
        response = await self.client.post(path, json=payload or PAYLOAD, headers=self.headers)
        return response, await response.text()

    def pause_old(self):
        bridge.private_json(self.bridge.quota_file, {'code': QUOTA_CODE, 'time': time.time() - 3600})

    def assert_success(self, text):
        self.assertEqual(text.count('event: message_start\n'), 1)
        self.assertEqual(text.count('event: message_stop\n'), 1)
        self.assertEqual(text.count('Recovered.'), 1)
        self.assertNotIn('event: error\n', text)

    async def test_http_quota_recovers_exact_original_request(self):
        self.actions.append(quota())
        # Hold the recovered response until the client observes two heartbeats.
        # Runner load can consume the short cooldown in a single scheduling turn.
        release = asyncio.Event()
        async def recovered(request):
            await asyncio.wait_for(release.wait(), 5)
            return await sse(request, [created(), delta(), complete()])
        self.actions.append(recovered)
        response = await self.client.post('/v1/messages', json=PAYLOAD, headers=self.headers)
        prefix = bytearray()
        try:
            async with asyncio.timeout(5):
                while prefix.count(b'event: ping\n') < 2:
                    line = await response.content.readline()
                    self.assertTrue(line, 'Stream ended before pending-request heartbeats arrived.')
                    prefix.extend(line)
        finally:
            release.set()
        text = (bytes(prefix) + await response.read()).decode()
        self.assertEqual(response.status, 200)
        self.assert_success(text)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertGreaterEqual(self.times[1] - self.times[0], .055)
        self.assertGreater(text.count('event: ping\n'), 1)
        self.assertFalse(self.bridge.quota_file.exists())
        self.assertEqual(self.bridge.auth.calls, 2)
        audit = (self.directory / 'usage-audit.jsonl').read_text()
        self.assertIn('quota_recovered', audit)
        self.assertNotIn('PRIVATE TEST PROMPT', audit)
        self.assertNotIn('fake-test-token', audit)

    async def test_created_then_stream_quota_does_not_duplicate_message(self):
        self.actions.append([created(), failed()])
        _, text = await self.send()
        self.assert_success(text)
        self.assertEqual(len(self.requests), 2)

    async def test_partial_text_quota_never_replays(self):
        self.actions.append([created(), delta(), failed()])
        _, text = await self.send()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(text.count('Recovered.'), 1)
        self.assertIn('event: error\n', text)
        self.assertTrue(self.bridge.quota_file.exists())

    async def test_partial_tool_quota_never_replays(self):
        self.actions.append([created(), {'type': 'response.output_item.added',
            'output_index': 0, 'item': {'type': 'function_call', 'call_id': 'call_1',
            'name': 'edit_file', 'arguments': ''}}, failed()])
        _, text = await self.send()
        self.assertEqual(len(self.requests), 1)
        self.assertIn('tool_use', text)
        self.assertIn('event: error\n', text)

    async def test_server_tool_before_quota_never_replays(self):
        self.actions.append([created(), {'type': 'response.web_search_call.in_progress'}, failed()])
        _, text = await self.send()
        self.assertEqual(len(self.requests), 1)
        self.assertIn('event: error\n', text)

    async def test_old_latch_gets_real_check_and_clears(self):
        self.pause_old()
        _, text = await self.send()
        self.assert_success(text)
        self.assertFalse(self.bridge.quota_file.exists())

    async def test_only_one_recovery_check_in_flight(self):
        self.pause_old()
        async def slow_quota(request):
            await asyncio.sleep(.05)
            self.assertEqual(len(self.requests), 1)
            return quota()
        self.actions.append(slow_quota)
        results = await asyncio.gather(*(self.send() for _ in range(4)))
        for _, text in results:
            self.assert_success(text)
        self.assertEqual(len(self.requests), 5)
        self.assertGreaterEqual(self.times[1] - self.times[0], .105)
        self.assertEqual(self.bridge.quota.waiters, 0)

    async def test_disconnect_during_cooldown_stops_rechecks(self):
        # Connection setup can exceed the short test cooldown on a busy runner.
        # Hold quota time still until the real client disconnect is acknowledged.
        quota_now = time.time()
        with patch.object(quota_recovery, 'time', SimpleNamespace(time=lambda: quota_now)):
            self.bridge.quota.pause({'retry-after': '.18'})
            response = await self.client.post('/v1/messages', json=PAYLOAD, headers=self.headers)
            await response.content.readline()
            self.assertEqual(self.bridge.quota.waiters, 1)
            response.close()
            async with asyncio.timeout(5):
                while self.bridge.quota.waiters:
                    await asyncio.sleep(.01)
            quota_now += 1
            await asyncio.sleep(.23)
            self.assertEqual(self.requests, [])
            self.assertEqual(self.bridge.quota.waiters, 0)

    async def test_nonstream_json_recovers(self):
        self.actions.append(quota())
        response, text = await self.send({**PAYLOAD, 'stream': False})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(text)['content'][0]['text'], 'Recovered.')
        self.assertEqual(len(self.requests), 2)

    async def test_count_tokens_works_during_quota_pause(self):
        self.bridge.quota.pause()
        response, text = await self.send(path='/v1/messages/count_tokens')
        self.assertEqual(response.status, 200)
        self.assertGreater(json.loads(text)['input_tokens'], 0)
        self.assertEqual(self.requests, [])

    async def test_auth_error_keeps_http_status_and_does_not_retry(self):
        self.actions.append(web.json_response({'error': {'message': 'Login expired'}}, status=401))
        response, text = await self.send()
        self.assertEqual(response.status, 401)
        self.assertEqual(response.headers['x-should-retry'], 'false')
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.bridge.quota_file.exists())

    async def test_auth_error_after_wait_uses_sse_error(self):
        self.pause_old()
        self.actions.append(web.json_response({'error': {'message': 'Login expired'}}, status=401))
        _, text = await self.send()
        self.assertIn('event: error\n', text)
        self.assertIn('Login expired', text)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.bridge.quota_file.exists())

    async def test_success_without_quota_unchanged(self):
        _, text = await self.send()
        self.assert_success(text)
        self.assertEqual(len(self.requests), 1)

    async def test_deferred_mcp_schema_loads_after_discovery_without_changing_prefix(self):
        tools = [
            {'name': 'ToolSearch', 'input_schema': {'type': 'object',
                'properties': {'query': {'type': 'string'}}}},
            {'name': 'mcp__blender__animate', 'defer_loading': True,
             'description': 'Synthetic animation tool', 'input_schema': {'type': 'object'}},
            {'name': 'mcp__browser__screenshot', 'defer_loading': True,
             'description': 'Synthetic screenshot tool', 'input_schema': {'type': 'object'}},
        ]
        payload = {**PAYLOAD, 'stream': False, 'tools': tools}
        call = {'type': 'function_call', 'call_id': 'call_search', 'name': 'ToolSearch',
                'arguments': '{"query":"select:mcp__blender__animate"}'}
        response = complete()
        response['response']['output'] = [call]
        self.actions.append([created(), response])
        reply, text = await self.send(payload)
        self.assertEqual(reply.status, 200)
        assistant = json.loads(text)
        self.assertEqual(assistant['stop_reason'], 'tool_use')
        payload['messages'] = [*payload['messages'],
            {'role': 'assistant', 'content': assistant['content']},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_search',
                'content': [{'type': 'tool_reference', 'tool_name': 'mcp__blender__animate'}]}]}]
        reply, _ = await self.send(payload)
        self.assertEqual(reply.status, 200)
        first, second = self.requests
        def schemas(request):
            return [tool['name'] for item in request['input']
                    if item.get('type') == 'additional_tools' for tool in item['tools']]
        self.assertEqual(schemas(first), ['ToolSearch'])
        self.assertEqual(schemas(second), ['ToolSearch', 'mcp__blender__animate'])
        self.assertEqual(second['input'][:len(first['input'])], first['input'])
        self.assertIn(call, second['input'])
        self.assertEqual(second['input'][-2]['type'], 'function_call_output')
        self.assertEqual(second['input'][-1]['type'], 'additional_tools')

    async def test_stale_success_cannot_clear_new_pause(self):
        self.pause_old()
        async def newer_limit(request):
            self.bridge.quota.pause()
            return await sse(request, [created(), delta(), complete()])
        self.actions.append(newer_limit)
        await self.send()
        self.assertTrue(self.bridge.quota_file.exists())

    async def test_shorter_error_does_not_override_provider_retry_after(self):
        state = self.bridge.quota.pause({'retry-after': '1800'})
        later = self.bridge.quota.pause({'retry-after': '300'})
        self.assertEqual(state['next_retry_at'], later['next_retry_at'])

    async def test_pending_request_is_converted_only_once(self):
        self.actions.append(quota())
        with patch.object(bridge, 'convert_request', wraps=bridge.convert_request) as convert:
            _, text = await self.send()
        self.assert_success(text)
        self.assertEqual(convert.call_count, 1)

    async def test_incomplete_response_does_not_clear_quota(self):
        self.pause_old()
        event = complete()
        event['type'] = 'response.incomplete'
        event['response']['incomplete_details'] = {'reason': 'max_output_tokens'}
        self.actions.append([created(), delta(), event])
        _, text = await self.send()
        self.assertIn('max_tokens', text)
        self.assertTrue(self.bridge.quota_file.exists())

    async def test_native_route_still_requires_claude_login(self):
        response, _ = await self.send({**PAYLOAD, 'model': 'claude-opus-test'})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.requests, [])


class RetryDelayTests(unittest.TestCase):
    def test_numeric_and_date_and_invalid_headers(self):
        self.assertEqual(retry_delay({'Retry-After': '1800'}, now=1000), 1800)
        self.assertEqual(retry_delay({'retry-after': formatdate(2800, usegmt=True)}, now=1000), 1800)
        for value in ('0', '-10', 'bad', 'nan', 'inf', None):
            self.assertEqual(retry_delay({'retry-after': value}, now=1000), 300)


if __name__ == '__main__':
    unittest.main(verbosity=2)
