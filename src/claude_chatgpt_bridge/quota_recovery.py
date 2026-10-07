"""Cancellable recovery for a live request; never starts background inference."""
import asyncio
import contextlib
from email.utils import parsedate_to_datetime
import json
import math
import time
import uuid

from aiohttp import web

QUOTA_CODE = 'subscription_sharing_usage_limit_exceeded'
RECHECK_SECONDS = 300


class QuotaWait(Exception):
    def __init__(self, headers=None, safe=True):
        self.headers = headers or {}
        self.safe = safe


def retry_delay(headers, minimum=RECHECK_SECONDS, now=None):
    """Honor Retry-After, without guessing a subscription's reset time."""
    now = time.time() if now is None else now
    value = headers.get('Retry-After') or headers.get('retry-after')
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, OverflowError, AttributeError):
            delay = minimum
    return max(minimum, delay) if math.isfinite(delay) else minimum


class QuotaGate:
    def __init__(self, path, write, audit, interval=RECHECK_SECONDS):
        self.path, self.write, self.audit = path, write, audit
        self.interval = interval
        self.lock = asyncio.Lock()
        self.waiters = 0

    def state(self):
        try:
            state = json.loads(self.path.read_text())
            # Migrate the old permanent latch using its actual recorded time.
            when = float(state['time'])
            retry_at = float(state.get('next_retry_at', when + self.interval))
            if not math.isfinite(when) or not math.isfinite(retry_at):
                raise ValueError('Invalid quota timestamp')
            return {**state, 'next_retry_at': retry_at}
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            # Repair invalid local state to a finite cooldown, not a busy loop.
            return self.pause()

    def pause(self, headers=None):
        now = time.time()
        next_retry = now + retry_delay(headers or {}, self.interval, now)
        try:
            previous = float(json.loads(self.path.read_text()).get('next_retry_at', 0))
            if math.isfinite(previous):
                next_retry = max(next_retry, previous)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        state = {'code': QUOTA_CODE, 'time': now, 'generation': uuid.uuid4().hex,
                 'next_retry_at': next_retry}
        self.write(self.path, state)
        self.audit('quota_paused', next_retry_at=state['next_retry_at'], code=QUOTA_CODE)
        return state

    @contextlib.asynccontextmanager
    async def slot(self, reply):
        if not self.path.exists():
            yield None
            return
        self.waiters += 1
        try:
            await reply.open()
            self.audit('quota_waiting')
            async with self.lock:
                while True:
                    state = self.state()
                    if state is None:
                        break
                    delay = state['next_retry_at'] - time.time()
                    if delay <= 0:
                        # Reserve the next check even if this request disconnects
                        # or the provider suffers a transport/authentication error.
                        state = {**state, 'generation': uuid.uuid4().hex,
                                 'next_retry_at': time.time() + self.interval}
                        self.write(self.path, state)
                        self.audit('quota_recheck')
                        yield state['generation']
                        return
                    await asyncio.sleep(min(delay, 10))
            # Ordinary requests can run concurrently once recovery is proven.
            yield None
        finally:
            self.waiters -= 1

    def completed(self, generation):
        state = self.state() if generation else None
        # An old in-flight success must not erase a newer upstream quota error.
        if state and state.get('generation') == generation:
            self.path.unlink(missing_ok=True)
            self.audit('quota_recovered')


class ReplyStream:
    def __init__(self, request, stream, heartbeat_seconds=10):
        self.request, self.stream = request, stream
        self.response = None
        self.content_started = False
        self.lock = asyncio.Lock()
        self.ping = None
        self.owner = asyncio.current_task()
        self.heartbeat_seconds = heartbeat_seconds

    async def open(self):
        if self.stream and self.response is None:
            self.response = web.StreamResponse(headers={
                'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache',
                'X-Local-Model-Route': 'chatgpt-subscription'})
            await self.response.prepare(self.request)
            self.ping = asyncio.create_task(self.heartbeat())

    async def emit(self, kind, data):
        if kind not in ('ping', 'error'):
            self.content_started = True
        if self.stream:
            await self.open()
            async with self.lock:
                event = {'type': kind, **data}
                await self.response.write(('event: ' + kind + '\ndata: ' +
                    json.dumps(event, separators=(',', ':')) + '\n\n').encode())

    async def heartbeat(self):
        try:
            # Flush headers promptly even when no model output is available.
            while True:
                await self.emit('ping', {})
                await asyncio.sleep(self.heartbeat_seconds)
        except (ConnectionError, RuntimeError):
            self.owner.cancel()

    async def finish(self, result):
        if self.response is None:
            return result
        if result is not self.response:
            # Once pings started, errors must use Anthropic's SSE error shape.
            detail = json.loads(result.body).get('error', {})
            await self.emit('error', {'error': detail})
        return self.response

    async def close(self):
        if self.ping:
            self.ping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.ping
        if self.response is not None:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self.response.write_eof()
