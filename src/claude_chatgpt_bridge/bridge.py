#!/usr/bin/env python3
"""A loopback Anthropic Messages adapter for ChatGPT plan usage.

Claude requests retain the client's own OAuth headers. ChatGPT requests use
separate official SIWC credentials. No fallback crosses the two subscriptions.
"""
import argparse
import asyncio
import base64
import contextlib
import copy
from collections import OrderedDict
import hashlib
import hmac
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import secrets
import time
import uuid

import aiohttp
from aiohttp import web
import tiktoken
from .auth import Auth, DEFAULT_DIR, RESOURCE, atomic_json
from .state import ensure_private_directory
from .quota_recovery import QuotaGate, QuotaWait, ReplyStream

ANTHROPIC = 'https://api.anthropic.com'
SIGNATURE = 'chatgpt-bridge-v1:'
KEY_HEADER = 'x-local-claude-bridge-key'
HOP_HEADERS = {'host', 'content-length', 'connection', 'transfer-encoding',
               'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'upgrade', 'content-encoding', KEY_HEADER}
ENCODER = tiktoken.get_encoding('o200k_base')
QUOTA_CODE = 'subscription_sharing_usage_limit_exceeded'
CACHE_MODELS = {'gpt-6.1-sol', 'gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'}
REPLAY_TYPE = 'bridge_response_output'
REQUEST_CLASSES = {'main', 'subagent', 'workflow', 'compaction', 'auxiliary'}
HELPER_MODELS = ('gpt-6-luna', 'gpt-5.6-luna')
TURN_STATE_HEADER = 'x-codex-turn-state'


def response_header(headers, name):
    if not hasattr(headers, 'items'):
        return None
    for key, value in headers.items():
        if key.lower() == name:
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            return value if isinstance(value, str) else None
    return None


def diagnostic_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}', value) else None


class TurnRouting:
    """Conversation identity plus server-issued, strictly turn-local hints.

    Mirrors Codex's session/thread headers and first-value-wins turn state.
    No identity is borrowed from another conversation. Tokens stay in bounded
    RAM, are never logged and cannot cross prompts, agents, models or auth.
    """
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        self.owner = None
        self.entries = OrderedDict()

    def prepare(self, incoming, model, authorization):
        owner = self.fingerprint(authorization)
        if owner != self.owner:
            self.entries.clear()
            self.owner = owner
        session = incoming.get('x-claude-code-session-id', '')
        prompt = incoming.get('x-claude-code-prompt-id', '')
        agent = incoming.get('x-claude-code-agent-id', '')
        category = incoming.get('x-claude-code-request-class', '')
        if not session:
            return {}, None
        def identity(parts):
            return str(uuid.uuid5(uuid.NAMESPACE_URL, 'claude-bridge:' + self.fingerprint(parts)))
        outgoing = {'session-id': identity([session, agent, category]),
                    'thread-id': identity([session, agent])}
        if not prompt or category not in REQUEST_CLASSES:
            return outgoing, None
        group = self.fingerprint([session, agent, category])
        turn = self.fingerprint([prompt, model])
        entry = self.entries.pop(group, None)
        if entry is None or entry['turn'] != turn:
            entry = {'turn': turn, 'token': None}
        self.entries[group] = entry
        while len(self.entries) > 128:
            self.entries.popitem(last=False)
        if entry['token'] is not None:
            outgoing[TURN_STATE_HEADER] = entry['token']
        return outgoing, (owner, group, entry)

    def remember(self, handle, value):
        if (handle is None or not isinstance(value, str) or not 0 < len(value) <= 8192
                or any(ord(char) < 32 or ord(char) > 126 for char in value)):
            return False
        owner, group, entry = handle
        if (owner != self.owner or self.entries.get(group) is not entry
                or entry['token'] is not None):
            return False
        entry['token'] = value
        return True


def estimate_image_tokens(part):
    """Conservative local reserve, never a claim of exact provider usage.

    Count image pixels rather than its base64 transport encoding. Do not
    resize the transmitted image or fetch remote URLs. Use unresized 32px
    patches and a conservative 2.5 multiplier (above the documented model
    multipliers) because the account route's exact preprocessing is unknown.
    Unknown dimensions reserve the full 30,000-patch image allowance.
    """
    patches = 30000
    url = part.get('image_url', '')
    if isinstance(url, str) and url.startswith('data:image/') and ';base64,' in url:
        try:
            from PIL import Image
        except ImportError:
            return math.ceil(patches * 2.5)
        try:
            raw = base64.b64decode(url.split(',', 1)[1], validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
            if width > 0 and height > 0:
                patches = min(patches, math.ceil(width / 32) * math.ceil(height / 32))
        except (ValueError, OSError, Image.DecompressionBombError):
            pass
    return math.ceil(patches * 2.5)


def estimated_tokens(value, *, compact=False):
    """Local text/schema count plus conservative image reserves.

    Only replace actual structured image content in the counting copy. Text
    containing a data URL and opaque reasoning retain their conservative text
    estimate. Actual provider usage remains authoritative. No request mutates.
    """
    image_reserve = 0
    def counting_copy(item):
        nonlocal image_reserve
        if isinstance(item, list):
            return [counting_copy(part) for part in item]
        if not isinstance(item, dict):
            return item
        if item.get('type') == 'input_image' and 'image_url' in item:
            image_reserve += estimate_image_tokens(item)
            return {**item, 'image_url': '[image counted separately]'}
        # Schemas are serialized text, including any example image objects.
        return {key: part if key in ('parameters', 'schema') else counting_copy(part)
                for key, part in item.items()}
    cleaned = counting_copy(value)
    raw = json.dumps(cleaned, ensure_ascii=False,
                     **({'sort_keys': True, 'separators': (',', ':')} if compact else {}))
    return len(ENCODER.encode(raw, disallowed_special=())) + image_reserve


def lightweight_helper(payload):
    """Bounded text-only auxiliary work; callers must check request class first.

    Desktop labels WebFetch extraction with the selected custom model, even
    though these are auxiliary requests. A Haiku-only check misses that work.
    """
    if (payload.get('tools') or payload.get('stop_sequences')
            or payload.get('max_tokens') == 1):
        return False
    # Do not change providers/models for large contexts, images, tools or a
    # reasoning transcript. Compaction is separately excluded by request class.
    content = [*blocks(payload.get('system')), *(b for m in payload.get('messages', [])
                                                for b in blocks(m.get('content')))]
    return (all(b.get('type') == 'text' for b in content)
            and sum(len(ENCODER.encode(b.get('text', ''), disallowed_special=())) for b in content) <= 16384)


class ResponseError(RuntimeError):
    def __init__(self, detail):
        self.code = detail.get('code', '')
        self.param = detail.get('param')
        self.status = {QUOTA_CODE: 429, 'subscription_sharing_usage_unavailable': 503,
            'subscription_sharing_user_not_eligible': 403,
            'subscription_sharing_unsupported_capability': 400,
            'subscription_sharing_route_not_supported': 403,
            'subscription_sharing_invalid_user': 401}.get(self.code, 502)
        self.kind = 'rate_limit_error' if self.status == 429 else 'api_error'
        super().__init__(detail.get('message') or self.code or 'ChatGPT request failed')


def private_json(path, value):
    atomic_json(path, value)

def blocks(content):
    return [{'type': 'text', 'text': content}] if isinstance(content, str) else content or []


def tool_name(name):
    if len(name) <= 64 and re.fullmatch(r'[a-zA-Z0-9_-]+', name):
        return name
    return 'tool_' + hashlib.sha256(name.encode()).hexdigest()[:48]


def pack_reasoning(item, model):
    return SIGNATURE + base64.urlsafe_b64encode(json.dumps(
        {'model': model, 'item': item}, separators=(',', ':')).encode()).decode()


def unpack_reasoning(block, model):
    signature = block.get('signature', '')
    if not signature.startswith(SIGNATURE):
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(signature[len(SIGNATURE):]))
        item = value['item']
        if value['model'] == model and item.get('type') == 'reasoning':
            return item
    except (ValueError, KeyError, TypeError):
        raise ValueError('The saved ChatGPT reasoning record is invalid.') from None
    return None


def visible_fingerprint(content):
    visible = [b for b in content if b.get('type') not in ('thinking', 'redacted_thinking')]
    return hashlib.sha256(json.dumps(visible, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def replay_block(response, names, model):
    # Preserve exact output items (including phase, argument strings and opaque
    # reasoning) in the client's existing non-visible thinking storage. The
    # visible fingerprint prevents replay from overriding edited chat content.
    item = {'type': REPLAY_TYPE, 'output': response.get('output', []),
            'visible_sha256': visible_fingerprint(response_content(response, names, model))}
    return {'type': 'thinking', 'thinking': '', 'signature': pack_reasoning(item, model)}


def restore_output(content, model):
    markers = [b for b in content if b.get('type') == 'thinking'
               and b.get('signature', '').startswith(SIGNATURE)]
    for marker in reversed(markers):
        try:
            value = json.loads(base64.urlsafe_b64decode(marker['signature'][len(SIGNATURE):]))
            item = value['item']
            if (value['model'] == model and item.get('type') == REPLAY_TYPE
                    and item.get('visible_sha256') == visible_fingerprint(content)
                    and isinstance(item.get('output'), list)):
                return copy.deepcopy(item['output'])
        except (ValueError, KeyError, TypeError):
            continue
    return None


def function_replays(messages, model, remembered=None):
    """Recover exact call records independently of assistant-block grouping.

    The client may split, regroup or omit the thinking block carrying a
    response's replay metadata. Each call is still uniquely identified by its
    call_id. A candidate is used only when its name and parsed arguments match
    the visible call; it never restores missing calls, text or reasoning.
    """
    found = dict(remembered or {})
    for message in messages:
        for block in blocks(message.get('content')):
            signature = block.get('signature', '')
            if not signature.startswith(SIGNATURE):
                continue
            try:
                value = json.loads(base64.urlsafe_b64decode(signature[len(SIGNATURE):]))
                record = value['item']
                if value['model'] != model or record.get('type') != REPLAY_TYPE:
                    continue
                for item in record.get('output', []):
                    if (not isinstance(item, dict) or item.get('type') != 'function_call'
                            or not isinstance(item.get('call_id'), str)
                            or not isinstance(item.get('name'), str)
                            or not isinstance(item.get('arguments'), str)):
                        continue
                    call_id = item['call_id']
                    if call_id in found and found[call_id] != item:
                        found[call_id] = None  # Conflicting records are not replayed.
                    else:
                        found[call_id] = item
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    return found


def restore_function_call(block, candidates):
    candidate = candidates.get(block['id'])
    if candidate and candidate.get('name') == tool_name(block['name']):
        try:
            if json.loads(candidate['arguments']) == block.get('input', {}):
                return copy.deepcopy(candidate)
        except (ValueError, KeyError, TypeError):
            pass
    return {'type': 'function_call', 'call_id': block['id'],
            'name': tool_name(block['name']), 'arguments': json.dumps(block.get('input', {}))}


def grouped_messages(messages):
    # Claude can store each streamed content block as a separate assistant
    # record. Reassemble adjacent assistant records before checking the marker.
    result = []
    for message in messages:
        content = list(blocks(message.get('content')))
        if message['role'] == 'assistant' and result and result[-1]['role'] == 'assistant':
            result[-1]['content'].extend(content)
        else:
            result.append({'role': message['role'], 'content': content})
    return result


def cache_parts(result):
    for item in result['input']:
        if item.get('type') == 'function_call_output':
            content = item.get('output', [])
        elif item.get('role') in ('developer', 'user') and item.get('type', 'message') == 'message':
            content = item.get('content', [])
        else:
            continue
        if isinstance(content, list):
            yield from (part for part in content if part.get('type') in ('input_text', 'input_image', 'input_file'))


def configure_cache(result, model):
    # Use provider-default caching. Explicit cache extensions are not portable
    # across subscription routes; stable prefixes do not guarantee cache hits.
    result.pop('prompt_cache_options', None)
    for part in cache_parts(result):
        part.pop('prompt_cache_breakpoint', None)


def checkpoint(part):
    part['prompt_cache_breakpoint'] = {'mode': 'explicit'}
    return part


def request_components(converted):
    tool_defs = list(converted.get('tools', []))
    instructions, conversation = [], []
    for item in converted['input']:
        if item.get('type') == 'additional_tools':
            tool_defs.extend(item['tools'])
        elif item.get('role') == 'developer':
            instructions.append(item)
        else:
            conversation.append(item)
    token_counts = {key: estimated_tokens(value)
                    for key, value in {'instructions': instructions, 'tools': tool_defs, 'input': conversation}.items()}
    return token_counts, tool_defs, instructions


def prefix_evidence(converted, key):
    """Only keyed hashes and token estimates; never store conversation text."""
    chain = hmac.new(key.encode(), digestmod=hashlib.sha256)
    points, total = [], 0
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k != 'prompt_cache_breakpoint'}
        if isinstance(value, list): return [clean(v) for v in value]
        return value
    chain.update(json.dumps(converted.get('tools', []), sort_keys=True).encode())
    for item in converted['input']:
        raw = json.dumps(clean(item), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        chain.update(raw.encode()); chain.update(b'\n')
        total += estimated_tokens(clean(item), compact=True)
        if item.get('role') in ('user', 'developer') or item.get('type') == 'function_call_output':
            points.append([chain.hexdigest(), total])
    return points[-256:], total


def request_prefix_trace(converted, fingerprint):
    """Compare wire prefixes without retaining prompts, images, or credentials.

    This is local evidence of an unchanged request prefix, not a cache hit.
    Hash whole input items: do not tokenize base64 images as ordinary text.
    """
    settings = {k: v for k, v in converted.items()
                if k not in ('input', 'stream', 'store', 'include')}
    return {'settings': fingerprint(settings),
            'items': [fingerprint(item) for item in converted['input']],
            'types': [item.get('type', 'message') if item.get('type', 'message') in
                      ('message', 'additional_tools', 'function_call', 'function_call_output',
                       'reasoning', 'web_search_call', 'configuration_update') else 'other'
                      for item in converted['input']]}


def compare_request_prefix(previous, current):
    if previous is None:
        return {'comparison_available': False}
    before, after = previous['items'], current['items']
    common = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b),
                  min(len(before), len(after)))
    settings_match = previous['settings'] == current['settings']
    return {'comparison_available': True, 'request_settings_unchanged': settings_match,
            'previous_input_items': len(before), 'matching_input_items': common,
            'previous_input_is_prefix': settings_match and common == len(before),
            'first_changed_item_type': current['types'][common]
                if common < min(len(before), len(after)) else None}


def native_payload(payload):
    """Drop tagged ChatGPT reasoning and obsolete native thinking after a switch.

    Keep current-turn native thinking for tool continuations; do not rewrite
    visible text, tool calls/results, the system prompt, or tool definitions.
    """
    messages = payload.get('messages', [])
    foreign = any(b.get('signature', '').startswith(SIGNATURE)
                  for m in messages for b in blocks(m.get('content')))
    if not foreign:
        return payload
    result = copy.deepcopy(payload)
    # A user tool_result belongs to an ongoing assistant turn, not a new turn.
    boundary = max((i for i, m in enumerate(messages) if m.get('role') == 'user'
                    and any(b.get('type') != 'tool_result' for b in blocks(m.get('content')))), default=0)
    for i, message in enumerate(result['messages']):
        if isinstance(message.get('content'), list):
            message['content'] = [b for b in message['content']
                if not b.get('signature', '').startswith(SIGNATURE)
                and not (i < boundary and b.get('type') in ('thinking', 'redacted_thinking'))]
    result['messages'] = [m for m in result['messages'] if m.get('content')]
    return result


def content_part(block, role):
    kind = block.get('type')
    if kind == 'text':
        return {'type': 'output_text' if role == 'assistant' else 'input_text',
                'text': block.get('text', '')}
    if kind == 'image':
        if role == 'assistant':
            raise ValueError('Assistant image blocks cannot be replayed through this adapter.')
        source = block['source']
        if source['type'] == 'base64':
            url = f"data:{source['media_type']};base64,{source['data']}"
        elif source['type'] == 'url':
            url = source['url']
        else:
            raise ValueError('Unsupported image source in Claude history.')
        return {'type': 'input_image', 'image_url': url}
    if kind == 'document':
        source = block.get('source', {})
        if source.get('type') == 'text':
            return {'type': 'input_text', 'text': source.get('data', '')}
        if source.get('type') == 'base64' and source.get('media_type') == 'application/pdf':
            return {'type': 'input_file', 'filename': 'document.pdf',
                    'file_data': 'data:application/pdf;base64,' + source['data']}
        raise ValueError('This document format cannot be replayed through the ChatGPT adapter.')
    if kind == 'tool_reference':
        return {'type': 'input_text', 'text': 'Available tool: ' + block.get('tool_name', '')}
    raise ValueError('Unsupported Claude history content: ' + str(kind))


def convert_request(payload, model, remembered_calls=None):
    result = {'model': model, 'input': [], 'stream': True, 'store': False,
              'include': ['reasoning.encrypted_content']}
    # Claude's attribution line contains per-request IDs/counters. It is only
    # meaningful to Anthropic, and changing it invalidates OpenAI's prefix cache.
    # Keep all actual system instructions and all user content unchanged.
    system = []
    for block in blocks(payload.get('system')):
        if block.get('type') != 'text':
            raise ValueError('Unsupported system content block.')
        text = re.sub(r'^x-anthropic-billing-header:[^\n]*(?:\n|$)', '', block['text'], count=1)
        if text:
            part = {'type': 'input_text', 'text': text}
            if block.get('cache_control'):
                checkpoint(part)
            system.append(part)
    if system:
        if not any(p.get('prompt_cache_breakpoint') for p in system):
            checkpoint(system[-1])
        result['input'].append({'role': 'developer', 'content': system})
    # Claude's own ToolSearch runs locally and enforces its normal tool policy.
    # It sends tool_reference results plus the newly selected schemas. Place
    # those schemas at discovery time instead of rewriting the cached prefix.
    function_defs = {}
    for tool in payload.get('tools', []):
        if 'input_schema' in tool:
            name = tool_name(tool['name'])
            function_defs[name] = {'type': 'function', 'name': name,
                'description': tool.get('description', ''), 'parameters': tool['input_schema'], 'strict': False}
    function_defs = json.loads(json.dumps(function_defs, sort_keys=True))
    has_search = 'ToolSearch' in function_defs
    forced = tool_name(payload.get('tool_choice', {}).get('name', ''))
    deferred = {tool_name(t['name']) for t in payload.get('tools', [])
                if has_search and t.get('defer_loading') and t['name'] != 'DeferredToolPlaceholder'
                and tool_name(t['name']) != forced}
    placeholder = {'DeferredToolPlaceholder'} if has_search else set()
    loaded = set(function_defs) - deferred - placeholder
    if loaded:
        result['input'].insert(0, {'type': 'additional_tools', 'role': 'developer',
                                   'tools': [function_defs[n] for n in sorted(loaded)]})

    def load_tools(names):
        pending = sorted({tool_name(n) for n in names} & deferred - loaded)
        if pending:
            result['input'].append({'type': 'additional_tools', 'role': 'developer',
                                    'tools': [function_defs[n] for n in pending]})
            loaded.update(pending)

    call_replays = function_replays(payload.get('messages', []), model, remembered_calls)
    for message in grouped_messages(payload.get('messages', [])):
        role = message['role']
        if role == 'assistant':
            restored = restore_output(message['content'], model)
            if restored is not None:
                for item in restored:
                    if item.get('type') == 'function_call':
                        load_tools([item['name']])
                    result['input'].append(item)
                continue
        parts = []
        def flush():
            if parts:
                result['input'].append({'role': role, 'content': parts.copy()})
                parts.clear()
        for block in blocks(message.get('content')):
            kind = block.get('type')
            if kind in ('thinking', 'redacted_thinking'):
                flush()
                own = unpack_reasoning(block, model)
                if own:
                    result['input'].append(own)
            elif kind == 'tool_use':
                flush()
                load_tools([block['name']])
                result['input'].append(restore_function_call(block, call_replays))
            elif kind == 'tool_result':
                flush()
                content = block.get('content', '')
                if isinstance(content, str):
                    output = [{'type': 'input_text', 'text':
                               ('[Tool execution failed]\n' if block.get('is_error') else '') + content}]
                else:
                    output = [content_part(b, 'user') for b in content]
                    if block.get('is_error'):
                        output.insert(0, {'type': 'input_text', 'text': '[Tool execution failed]'})
                if output:
                    checkpoint(output[-1])
                result['input'].append({'type': 'function_call_output',
                    'call_id': block['tool_use_id'], 'output': output})
                if isinstance(content, list):
                    load_tools([b.get('tool_name', '') for b in content if b.get('type') == 'tool_reference'])
            elif kind == 'tool_reference':
                flush()
                load_tools([block.get('tool_name', '')])
                parts.append(content_part(block, role))
            elif kind in ('server_tool_use', 'web_search_tool_result', 'web_fetch_tool_result'):
                # Hosted-tool records from the other provider are contextual data.
                parts.append({'type': 'input_text' if role == 'user' else 'output_text',
                              'text': json.dumps(block, ensure_ascii=False)})
            elif kind in ('compaction', 'context_management'):
                raise ValueError('This provider-specific compaction block cannot be replayed. Use a visible conversation summary.')
            else:
                part = content_part(block, role)
                if role == 'user' and block.get('cache_control'):
                    checkpoint(part)
                parts.append(part)
        flush()
    tools, names = [], {}
    for tool in payload.get('tools', []):
        if tool.get('type', '').startswith('web_search_'):
            entry = {'type': 'web_search'}
            if tool.get('blocked_domains'):
                raise ValueError('The ChatGPT web-search adapter does not support blocked_domains.')
            if tool.get('allowed_domains'):
                entry['filters'] = {'allowed_domains': tool['allowed_domains']}
            tools.append(entry)
            result['include'].append('web_search_call.action.sources')
        elif tool.get('type', '').startswith('tool_search_'):
            # All function schemas are supplied below, including deferred schemas.
            continue
        elif 'input_schema' in tool:
            name = tool_name(tool['name']); names[name] = tool['name']
        else:
            raise ValueError('Unsupported server-side tool: ' + str(tool.get('type', tool.get('name'))))
    if tools:
        hosted = [tool for tool in tools if tool['type'] != 'function']
        if hosted:
            result['tools'] = hosted
    choice = payload.get('tool_choice', {})
    if choice.get('type') == 'tool':
        result['tool_choice'] = {'type': 'function', 'name': tool_name(choice['name'])}
    elif choice.get('type') in ('auto', 'none', 'any'):
        result['tool_choice'] = {'any': 'required'}.get(choice['type'], choice['type'])
    if choice.get('disable_parallel_tool_use'):
        result['parallel_tool_calls'] = False
    effort = payload.get('output_config', {}).get('effort', 'medium')
    result['reasoning'] = {'effort': effort if effort in ('low', 'medium', 'high', 'xhigh', 'max') else 'high'}
    if model == 'gpt-6.1-sol' or model.startswith('gpt-5.6-'):
        result['reasoning']['context'] = 'all_turns'
    # SIWC does not accept max_output_tokens. Do not send the Anthropic limit
    # under that unsupported field or claim that it is enforced upstream.
    fmt = payload.get('output_config', {}).get('format')
    if fmt and fmt.get('type') == 'json_schema':
        result['text'] = {'format': {'type': 'json_schema', 'name': 'claude_output',
                                   'schema': fmt['schema'], 'strict': True}}
    # Never silently discard a truncation strategy or stop constraint.
    if payload.get('stop_sequences'):
        raise ValueError('Custom stop_sequences are not supported by the ChatGPT Responses adapter. '
                         'For Claude Code tool approvals, use Manual mode (Shift+Tab) or launch claude-chatgpt.')
    configure_cache(result, model)
    return result, names


def usage(response):
    value = response.get('usage') or {}
    cached = (value.get('input_tokens_details') or {}).get('cached_tokens', 0)
    written = (value.get('input_tokens_details') or {}).get('cache_write_tokens', 0)
    return {'input_tokens': max(0, value.get('input_tokens', 0) - cached - written),
            'cache_read_input_tokens': cached, 'cache_creation_input_tokens': written,
            'output_tokens': value.get('output_tokens', 0)}


def response_content(response, names, model):
    content = []
    for item in response.get('output', []):
        kind = item['type']
        if kind == 'reasoning' and item.get('encrypted_content'):
            content.append({'type': 'thinking', 'thinking': '', 'signature': pack_reasoning(item, model)})
        elif kind == 'message':
            for part in item.get('content', []):
                if part.get('type') in ('output_text', 'refusal'):
                    content.append({'type': 'text', 'text': part.get('text', part.get('refusal', ''))})
        elif kind == 'function_call':
            content.append({'type': 'tool_use', 'id': item['call_id'],
                            'name': names.get(item['name'], item['name']),
                            'input': json.loads(item['arguments'])})
    citations = response_sources(response)
    if citations:
        content.append({'type': 'text', 'text': citations})
    return content


def response_sources(response):
    sources = {}
    for item in response.get('output', []):
        for part in item.get('content', []):
            for annotation in part.get('annotations', []):
                if annotation.get('type') == 'url_citation':
                    url = annotation.get('url', '')
                    if url.startswith(('https://', 'http://')):
                        sources[url] = annotation.get('title') or url
        if item.get('type') == 'web_search_call':
            for source in item.get('action', {}).get('sources', []):
                url = source.get('url', '')
                if url.startswith(('https://', 'http://')):
                    sources.setdefault(url, source.get('title') or url)
    return ('\n\nSources:\n' + '\n'.join(f'- [{title}]({url})' for url, title in sources.items())) if sources else ''


class Translator:
    def __init__(self, emit, alias, model, names):
        self.emit, self.alias, self.model, self.names = emit, alias, model, names
        self.indices, self.closed = {}, set()
        self.next_index = 0
        self.message_id = 'msg_chatgpt_' + uuid.uuid4().hex
        self.started = False
        self.final = None
        self.failed = False
        self.completed_items = {}

    async def start(self):
        if not self.started:
            self.started = True
            await self.emit('message_start', {'message': {'id': self.message_id,
                'type': 'message', 'role': 'assistant', 'model': self.alias,
                'content': [], 'stop_reason': None, 'stop_sequence': None,
                'usage': {'input_tokens': 0, 'output_tokens': 0}}})

    async def block(self, key, value):
        await self.start()
        if key not in self.indices:
            index = self.next_index; self.next_index += 1; self.indices[key] = index
            await self.emit('content_block_start', {'index': index, 'content_block': value})
        return self.indices[key]

    async def close(self, key):
        if key in self.indices and key not in self.closed:
            await self.emit('content_block_stop', {'index': self.indices[key]}); self.closed.add(key)

    async def event(self, event):
        kind = event.get('type', '')
        item = event.get('item', {})
        if kind == 'response.output_item.done':
            self.completed_items[event['output_index']] = item
        if kind in ('response.created', 'response.in_progress'):
            # Admission can fail after these events. Defer message_start until
            # real output so a quota retry does not create two messages.
            pass
        elif kind == 'response.output_item.added' and item.get('type') == 'function_call':
            await self.block(('tool', event['output_index']), {'type': 'tool_use',
                'id': item['call_id'], 'name': self.names.get(item['name'], item['name']), 'input': {}})
        elif kind in ('response.output_text.delta', 'response.refusal.delta'):
            key = ('text', event['output_index'], event.get('content_index', 0))
            index = await self.block(key, {'type': 'text', 'text': ''})
            await self.emit('content_block_delta', {'index': index,
                'delta': {'type': 'text_delta', 'text': event['delta']}})
        elif kind in ('response.output_text.done', 'response.refusal.done'):
            await self.close(('text', event['output_index'], event.get('content_index', 0)))
        elif kind == 'response.function_call_arguments.delta':
            index = self.indices[('tool', event['output_index'])]
            await self.emit('content_block_delta', {'index': index,
                'delta': {'type': 'input_json_delta', 'partial_json': event['delta']}})
        elif kind == 'response.function_call_arguments.done':
            await self.close(('tool', event['output_index']))
        elif kind == 'response.output_item.done' and item.get('type') == 'reasoning' and item.get('encrypted_content'):
            key = ('reasoning', event['output_index'])
            index = await self.block(key, {'type': 'thinking', 'thinking': '', 'signature': ''})
            await self.emit('content_block_delta', {'index': index,
                'delta': {'type': 'signature_delta', 'signature': pack_reasoning(item, self.model)}})
            await self.close(key)
        elif kind in ('response.completed', 'response.incomplete'):
            self.completed = kind == 'response.completed'
            response = event['response']
            self.raw_usage = response.get('usage') or {}
            self.response_id = response.get('id')
            # SIWC may send completed output items only in item.done events,
            # leaving the final response envelope's output array empty.
            if not response.get('output') and self.completed_items:
                response = {**response, 'output': [self.completed_items[i]
                            for i in sorted(self.completed_items)]}
            if kind == 'response.incomplete' and response.get('incomplete_details', {}).get('reason') != 'max_output_tokens':
                raise RuntimeError('ChatGPT returned an incomplete response.')
            await self.start()
            for key in self.indices:
                await self.close(key)
            citations = response_sources(response)
            if citations:
                key = ('sources',)
                index = await self.block(key, {'type': 'text', 'text': ''})
                await self.emit('content_block_delta', {'index': index,
                    'delta': {'type': 'text_delta', 'text': citations}})
                await self.close(key)
            reason = 'max_tokens' if kind == 'response.incomplete' else (
                'tool_use' if any(i['type'] == 'function_call' for i in response.get('output', [])) else 'end_turn')
            state = []
            # Claude takes its CLI result from the last streamed block. A
            # trailing thinking-only block would hide a terminal text answer.
            # Preserve exact state on tool continuations; terminal answers keep
            # their visible text and separately stored encrypted reasoning.
            if reason == 'tool_use':
                state = [replay_block(response, self.names, self.model)]
                state_index = await self.block(('replay',), {'type': 'thinking', 'thinking': '', 'signature': ''})
                await self.emit('content_block_delta', {'index': state_index,
                    'delta': {'type': 'signature_delta', 'signature': state[0]['signature']}})
                await self.close(('replay',))
            self.final = {'id': self.message_id, 'type': 'message', 'role': 'assistant',
                          'model': self.alias, 'content': [*response_content(response, self.names, self.model), *state],
                          'stop_reason': reason, 'stop_sequence': None, 'usage': usage(response)}
            await self.emit('message_delta', {'delta': {'stop_reason': reason, 'stop_sequence': None},
                                              'usage': usage(response)})
            await self.emit('message_stop', {})
        elif kind in ('response.failed', 'error'):
            error = event.get('error') or event.get('response', {}).get('error') or {}
            raise ResponseError(error)


def error_response(status, message, error_type='api_error', code=None, retry=None):
    detail = {'type': error_type, 'message': message}
    if code:
        detail['code'] = code
    return web.json_response({'type': 'error', 'error': detail}, status=status,
        headers={'x-should-retry': 'true' if retry else 'false'} if retry is not None else None)


class Bridge:
    def __init__(self, directory, port, *, allow_claude=False):
        self.directory, self.port = ensure_private_directory(directory), port
        self.allow_claude = allow_claude
        self.key = (self.directory / 'bridge.key').read_text().strip()
        desktop_key = self.directory / 'desktop.key'
        self.desktop_key = desktop_key.read_text().strip() if desktop_key.exists() else None
        if self.desktop_key is not None and len(self.desktop_key) < 32:
            raise ValueError('Invalid desktop bridge credential.')
        if len(self.key) < 16:
            raise ValueError('Invalid local bridge credential; run claude-chatgpt setup.')
        self.auth = Auth(self.directory)
        self.models = json.loads((self.directory / 'models.json').read_text()) if (self.directory / 'models.json').exists() else []
        self.aliases = {'chatgpt.' + row['slug']: row['slug'] for row in self.models}
        self.main_models = {}
        self.last_prefixes = {}
        self.call_replays = OrderedDict()
        self.call_replay_bytes = 0
        self.turn_routing = TurnRouting(self.fingerprint)
        self.session = None
        self.quota_file = self.directory / 'quota-paused.json'
        self.quota = QuotaGate(self.quota_file, private_json, self.audit)
        self.audit_handler = None
        policy = self.directory / 'efficiency.json'
        self.policy = json.loads(policy.read_text()) if policy.exists() else {}
        if self.policy.get('compaction', 'auto') not in ('auto', 'main', 'luna'):
            raise ValueError('Compaction policy must be auto, main, or luna.')
        self.cache_file = self.directory / 'cache-evidence.json'
        try:
            self.cache_evidence = json.loads(self.cache_file.read_text())
        except (OSError, ValueError):
            self.cache_evidence = {}

    def helper_alias(self):
        return next(('chatgpt.' + m for m in HELPER_MODELS if 'chatgpt.' + m in self.aliases), None)

    def compaction_route(self, payload, alias, session_id, converted):
        policy, helper = self.policy.get('compaction', 'auto'), self.helper_alias()
        if policy == 'main' or not helper or helper == alias:
            return alias, converted, 'compaction_main'
        if any(b.get('type') in ('image', 'document') or
               (b.get('type') == 'tool_result' and isinstance(b.get('content'), list) and
                any(p.get('type') in ('image', 'document') for p in b['content']))
               for m in payload.get('messages', []) for b in blocks(m.get('content'))):
            return alias, converted, 'compaction_multimodal_main'
        points, total = prefix_evidence(converted, self.key)
        evidence = self.cache_evidence.get(self.fingerprint([session_id, self.aliases[alias]]), {})
        previous = {p[0]: p[1] for p in evidence.get('prefixes', [])}
        matching = max((n for digest, n in points if digest in previous), default=0)
        # A recent matching prefix with observed cache reads/writes is evidence,
        # not a guarantee of a hit. Reuse it when most of this request matches.
        if (policy == 'auto' and session_id and evidence.get('expires_at', 0) > time.time()
                and evidence.get('effort') == converted.get('reasoning', {}).get('effort')
                and matching >= max(1024, total * .75)
                and evidence.get('covered_tokens', 0) >= total * .75):
            return alias, converted, 'compaction_warm_main'
        candidate, _ = convert_request(payload, self.aliases[helper])
        _, size = prefix_evidence(candidate, self.key)
        metadata = next(m for m in self.models if m['slug'] == self.aliases[helper])
        window = metadata.get('context_window', 0)
        # Use the account's ordinary window, retain all input and reserve room
        # for a careful summary. Never truncate to force a smaller model to fit.
        if not window or math.ceil(size * 1.15) + 16384 > window:
            return alias, converted, 'compaction_context_main'
        candidate['reasoning']['effort'] = 'medium'
        return helper, candidate, 'compaction_cold_luna' if policy == 'auto' else 'compaction_luna'

    def remember_cache(self, session_id, converted, details):
        covered = details.get('cached_tokens', 0) + details.get('cache_write_tokens', 0)
        if not session_id or not covered or converted['model'] not in CACHE_MODELS:
            return
        points, _ = prefix_evidence(converted, self.key)
        now = time.time()
        self.cache_evidence = {k: v for k, v in self.cache_evidence.items() if v.get('expires_at', 0) > now}
        self.cache_evidence[self.fingerprint([session_id, converted['model']])] = {
            # No explicit TTL is requested on this subscription route. Treat
            # even observed cache reuse as short-lived evidence, not a promise.
            'expires_at': now + 4 * 60, 'effort': converted.get('reasoning', {}).get('effort'),
            'covered_tokens': covered, 'prefixes': points}
        self.cache_evidence = dict(list(self.cache_evidence.items())[-128:])
        private_json(self.cache_file, self.cache_evidence)

    def fingerprint(self, value):
        raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        return hmac.new(self.key.encode(), raw, hashlib.sha256).hexdigest()[:20]

    def remember_function_calls(self, scope, content, model):
        if scope is None:
            return
        for call_id, item in function_replays([{'content': content}], model).items():
            if item is None:
                continue
            size = len(json.dumps(item).encode())
            if size > 256 * 1024:
                continue
            key = (scope, call_id)
            old = self.call_replays.pop(key, None)
            if old:
                self.call_replay_bytes -= old[1]
            self.call_replays[key] = (copy.deepcopy(item), size)
            self.call_replay_bytes += size
            while len(self.call_replays) > 256 or self.call_replay_bytes > 2 * 1024**2:
                _, (_, removed_size) = self.call_replays.popitem(last=False)
                self.call_replay_bytes -= removed_size

    def audit(self, event, **fields):
        # Only caller-selected counts, hashes, model IDs and error codes. Never
        # record prompts, tool arguments/results, credentials or upstream bodies.
        if self.audit_handler:
            # Provider error codes and IDs can contain arbitrary text. Retain
            # only known codes, numeric counts, enums and keyed fingerprints.
            known_codes = {QUOTA_CODE, 'subscription_sharing_usage_unavailable',
                'subscription_sharing_user_not_eligible', 'subscription_sharing_unsupported_capability',
                'subscription_sharing_route_not_supported', 'subscription_sharing_invalid_user',
                'invalid_request_error', 'rate_limit_exceeded', 'server_error'}
            for key in ('response', 'upstream_request_id'):
                if fields.get(key):
                    fields[key] = self.fingerprint(fields[key])
            if fields.get('code') and fields['code'] not in known_codes:
                fields['code'] = 'other'
            for key in ('model', 'requested_model', 'server_model', 'server_model_header'):
                if key in fields and fields[key] not in self.aliases and fields[key] not in self.aliases.values():
                    fields[key] = 'other'
            numeric = ('requested_max_tokens', 'input_tokens', 'cached_tokens',
                       'cache_write_tokens', 'output_tokens', 'reasoning_tokens')
            for key in numeric:
                if key in fields and type(fields[key]) is not int:
                    fields[key] = None
            line = json.dumps({'time': time.time(), 'event': event, **fields}, separators=(',', ':'))
            self.audit_handler.emit(logging.makeLogRecord({'msg': line, 'levelno': logging.INFO}))

    def quota_response(self):
        return error_response(429, 'ChatGPT plan usage limit reached after output had already started. '
            'Automatic replay stopped to avoid duplicating actions. Retry once to wait for allowance; '
            'pending requests then recheck automatically. Usage: https://chatgpt.com/settings/usage',
            'rate_limit_error', QUOTA_CODE, retry=False)

    def credential_kind(self, request):
        if KEY_HEADER in request.headers:
            return 'bridge' if secrets.compare_digest(request.headers[KEY_HEADER], self.key) else None
        if not self.desktop_key:
            return None
        candidates = []
        if 'Authorization' in request.headers:
            scheme, _, value = request.headers['Authorization'].partition(' ')
            if scheme.lower() != 'bearer':
                return None
            candidates.append(value)
        if 'x-api-key' in request.headers:
            candidates.append(request.headers['x-api-key'])
        if candidates and all(secrets.compare_digest(v, self.desktop_key) for v in candidates):
            return 'desktop'
        return None

    def authorized(self, request):
        return self.credential_kind(request) is not None

    async def setup(self, app):
        audit_path = self.directory / 'usage-audit.jsonl'
        fd = os.open(audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        os.chmod(audit_path, 0o600)
        self.audit_handler = RotatingFileHandler(audit_path, maxBytes=2 * 1024**2, backupCount=2)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1200, sock_connect=30, sock_read=1200),
                                           read_bufsize=1024 * 1024)
        yield
        await self.session.close()
        self.audit_handler.close()

    async def health(self, request):
        if self.credential_kind(request) != 'bridge' or request.headers.get('Origin'):
            return error_response(401, 'Missing local adapter credential.', 'authentication_error')
        state = self.quota.state()
        return web.json_response({'ok': True, 'service': 'claude-chatgpt-bridge',
            'subscription_models': len(self.models), 'quota_recovery': {
                'automatic': True, 'paused': state is not None,
                'waiting_requests': self.quota.waiters,
                'next_check_at': state.get('next_retry_at') if state else None}})

    async def handle(self, request):
        if not self.authorized(request):
            return error_response(401, 'Missing local adapter credential.', 'authentication_error')
        if request.headers.get('Origin'):
            return error_response(403, 'Browser cross-origin requests are not accepted.')
        try:
            if request.method == 'GET' and request.path == '/v1/models':
                return web.json_response({'data': [{'id': k, 'type': 'model',
                    'display_name': next(m['display_name'] for m in self.models if m['slug'] == v)}
                    for k, v in self.aliases.items()], 'has_more': False})
            if request.method != 'POST' or request.path not in ('/v1/messages', '/v1/messages/count_tokens'):
                return error_response(404, 'This endpoint is not served by the local adapter.', 'not_found_error')
            raw = await request.read()
            payload = json.loads(raw)
            alias = payload.get('model', '')
            session_id = request.headers.get('x-claude-code-session-id', '')
            category = request.headers.get('x-claude-code-request-class', '')
            if category == 'main' and session_id:
                self.main_models[session_id] = alias
                if len(self.main_models) > 1000:
                    self.main_models.pop(next(iter(self.main_models)))
            inherited = self.main_models.get(session_id, '')
            # One-token model-validation calls must test the requested provider,
            # even when the preceding main turn used a different subscription.
            routing = 'selected_model'
            if category == 'auxiliary' and payload.get('max_tokens') != 1:
                if inherited in self.aliases and alias.startswith('claude-'):
                    alias = inherited
                    routing = 'main_model_helper'
                helper = self.helper_alias()
                if alias in self.aliases and helper and lightweight_helper(payload):
                    alias = helper
                    routing = 'lightweight_helper'
            if alias in self.aliases:
                return await self.chatgpt(request, payload, alias, routing)
            if alias.startswith('claude-'):
                if not self.allow_claude or self.credential_kind(request) == 'desktop':
                    return error_response(401, 'Claude forwarding is disabled. Use Claude Code directly or explicitly enable forwarding.', 'authentication_error')
                return await self.native(request, payload, raw)
            return error_response(400, 'Unknown model: ' + str(alias), 'invalid_request_error')
        except (ValueError, KeyError, TypeError) as error:
            return error_response(400, str(error), 'invalid_request_error')
        except RuntimeError as error:
            return error_response(503, str(error))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return error_response(502, 'The selected subscription could not be reached. No other provider was used.')

    async def native(self, request, payload, raw):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer ') or secrets.compare_digest(auth[7:], self.key):
            return error_response(401, 'Claude subscription login is required. Run claude auth login.', 'authentication_error')
        outgoing = native_payload(payload)
        body = raw if outgoing is payload else json.dumps(outgoing).encode()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS
                   and k.lower() not in ('x-api-key', 'accept-encoding')}
        headers['Accept-Encoding'] = 'identity'
        async with self.session.post(ANTHROPIC + request.path, data=body, headers=headers,
                                     allow_redirects=False) as upstream:
            response = web.StreamResponse(status=upstream.status, headers={
                k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS})
            response.headers['X-Local-Model-Route'] = 'claude-subscription'
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
            except ConnectionResetError:
                pass
            return response

    async def chatgpt(self, request, payload, alias, routing='selected_model'):
        reply = ReplyStream(request, payload.get('stream', False)
                            and not request.path.endswith('/count_tokens'))
        try:
            if request.path.endswith('/count_tokens'):
                return await self.chatgpt_attempt(request, payload, alias, routing, reply)
            while True:
                try:
                    async with self.quota.slot(reply) as generation:
                        request['bridge_completed'] = False
                        result = await self.chatgpt_attempt(request, payload, alias, routing, reply)
                        if request.get('bridge_completed'):
                            self.quota.completed(generation)
                        return await reply.finish(result)
                except QuotaWait as error:
                    self.quota.pause(error.headers)
                    if not error.safe or reply.content_started:
                        self.audit('quota_replay_stopped', reason='partial_output')
                        return await reply.finish(self.quota_response())
                    # The original payload stays only in this live handler.
                    # Disconnect/cancel drops it and stops all automatic checks.
        except (ValueError, KeyError, TypeError) as error:
            return await reply.finish(error_response(400, str(error), 'invalid_request_error'))
        except RuntimeError as error:
            return await reply.finish(error_response(503, str(error)))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return await reply.finish(error_response(502, 'The selected subscription could not be reached.'))
        except ConnectionResetError:
            self.audit('client_disconnected', model=self.aliases[alias])
            return reply.response
        finally:
            await reply.close()

    async def chatgpt_attempt(self, request, payload, alias, routing, reply):
        if 'bridge_prepared' not in request:
            model = self.aliases[alias]
            category = request.headers.get('x-claude-code-request-class', '')
            session_id = request.headers.get('x-claude-code-session-id', '')
            replay_scope = self.fingerprint([session_id, category,
                request.headers.get('x-claude-code-agent-id', ''), model]) if session_id else None
            remembered = {call_id: item for (scope, call_id), (item, _) in self.call_replays.items()
                          if scope == replay_scope} if replay_scope else None
            converted, names = convert_request(payload, model, remembered)
            if routing == 'lightweight_helper':
                converted['reasoning']['effort'] = 'low'
            if category == 'compaction' and payload.get('max_tokens') != 1:
                alias, converted, routing = self.compaction_route(payload, alias, session_id, converted)
                model = self.aliases[alias]
            request['bridge_prepared'] = (model, category, session_id, replay_scope, converted, names, alias, routing)
        model, category, session_id, replay_scope, converted, names, alias, routing = request['bridge_prepared']
        if request.path.endswith('/count_tokens'):
            # Local estimate, including tool schemas. Actual usage comes from OpenAI.
            count = estimated_tokens(converted)
            return web.json_response({'input_tokens': math.ceil(count * 1.08) + 64},
                                     headers={'X-Local-Token-Count': 'estimate'})
        # Keep cache accounting tied to the conversation across user prompts,
        # helper calls and restarts. Never derive this key from the changing
        # prompt ID, current message text, or rotating OAuth access token.
        # This supported field is separate from the rejected cache options;
        # it does not force a hit or change the provider's cache lifetime.
        if session_id:
            converted['prompt_cache_key'] = 'claude-bridge-v1:' + self.fingerprint(session_id)
        trace = request_prefix_trace(converted, self.fingerprint)
        trace_key = self.fingerprint([session_id, category,
            request.headers.get('x-claude-code-agent-id', ''), model]) if session_id else None
        comparison = compare_request_prefix(self.last_prefixes.get(trace_key), trace)
        headers = await self.auth.headers(self.session)
        routing_headers, routing_handle = self.turn_routing.prepare(
            request.headers, model, headers.get('Authorization', ''))
        headers.update(routing_headers)
        request_id = uuid.uuid4().hex
        components, tool_defs, instructions = request_components(converted)
        request['bridge_request_id'] = request_id
        request['bridge_model'] = model
        self.audit('upstream_started', request=request_id, model=model,
            requested_model=payload.get('model'), routing=routing,
            session=self.fingerprint(request.headers.get('x-claude-code-session-id', '')),
            prompt=self.fingerprint(request.headers.get('x-claude-code-prompt-id', '')),
            request_class=request.headers.get('x-claude-code-request-class', '')
                if request.headers.get('x-claude-code-request-class', '') in REQUEST_CLASSES else 'other',
            model_probe=payload.get('max_tokens') == 1, estimate_tokens=components,
            requested_max_tokens=payload.get('max_tokens'),
            tools=len(tool_defs), input_items=len(converted['input']),
            tool_definitions_received=len(payload.get('tools', [])),
            deferred_tools_received=sum(bool(t.get('defer_loading')) for t in payload.get('tools', [])),
            reasoning_effort=converted.get('reasoning', {}).get('effort'),
            cache_mode=converted.get('prompt_cache_options', {}).get('mode', 'provider_default'),
            cache_ttl=converted.get('prompt_cache_options', {}).get('ttl'),
            explicit_breakpoints=sum(bool(p.get('prompt_cache_breakpoint')) for p in cache_parts(converted)),
            cache_key_mode='conversation' if session_id else 'omitted',
            conversation_headers_sent='session-id' in routing_headers,
            turn_state_sent=TURN_STATE_HEADER in routing_headers,
            prefix_comparison=comparison,
            instructions_hash=self.fingerprint(instructions),
            tools_hash=self.fingerprint(tool_defs), payload_hash=self.fingerprint(converted))
        async with self.session.post(RESOURCE + '/responses', json=converted,
                                     headers=headers, allow_redirects=False) as upstream:
            output_observed = False
            response_info = {
                'upstream_request_id': diagnostic_id(upstream.headers.get('x-request-id')),
                'server_model_header': diagnostic_id(upstream.headers.get('openai-model')
                    or upstream.headers.get('x-openai-model')),
                'server_model': None, 'turn_state_received': False, 'turn_state_learned': False}
            def observe_headers(values):
                token = response_header(values, TURN_STATE_HEADER)
                if token is not None:
                    response_info['turn_state_received'] = True
                    learned = self.turn_routing.remember(routing_handle, token)
                    response_info['turn_state_learned'] |= learned
                reported = diagnostic_id(response_header(values, 'openai-model')
                    or response_header(values, 'x-openai-model'))
                if reported:
                    response_info['server_model_header'] = reported
            def observe_event(event):
                nonlocal output_observed
                if event.get('type', '').startswith(('response.output_', 'response.function_',
                        'response.web_search_', 'response.code_interpreter_', 'response.mcp_')):
                    output_observed = True
                if event.get('type') == 'response.metadata':
                    observe_headers(event.get('headers'))
                envelope = event.get('response')
                if isinstance(envelope, dict):
                    reported = diagnostic_id(envelope.get('model'))
                    if reported:
                        response_info['server_model'] = reported
                    # Model headers can also arrive in standard Responses events.
                    reported = diagnostic_id(response_header(envelope.get('headers'), 'openai-model')
                        or response_header(envelope.get('headers'), 'x-openai-model'))
                    if reported:
                        response_info['server_model_header'] = reported
            if upstream.status != 200:
                try:
                    error = await upstream.json(content_type=None)
                    detail = error.get('error', {'message': error.get('detail', 'ChatGPT rejected the request.')}) if isinstance(error, dict) else {}
                    message = detail.get('message', 'ChatGPT rejected the request.') if isinstance(detail, dict) else str(detail)
                except (ValueError, TypeError):
                    message = 'ChatGPT rejected the request.'
                    detail = {}
                code = detail.get('code') if isinstance(detail, dict) else None
                param = detail.get('param') if isinstance(detail, dict) else None
                # Restrict parameter logging to known schema fields, never
                # arbitrary provider text which could contain private input.
                self.audit('upstream_error', request=request_id, model=model, status=upstream.status, code=code,
                    **response_info,
                    param=param if param in ('prompt_cache_options', 'prompt_cache_breakpoint',
                        'reasoning.context', 'reasoning.effort', 'tools', 'input') else None)
                if code == QUOTA_CODE:
                    raise QuotaWait(upstream.headers)
                response = error_response(upstream.status, message,
                    'rate_limit_error' if upstream.status == 429 else 'api_error', code=code,
                    retry=False if upstream.status in (400, 401, 403, 404, 422) else None)
                for name in ('retry-after', 'x-request-id'):
                    if name in upstream.headers:
                        response.headers[name] = upstream.headers[name]
                response.headers['X-Local-Model-Route'] = 'chatgpt-subscription'
                return response
            observe_headers(upstream.headers)
            stream = payload.get('stream', False)
            await reply.open()
            emit = reply.emit
            translator = Translator(emit, alias, model, names)
            try:
                # SSE lines can span network chunks; aiohttp's line iterator joins them.
                pending = []
                async for line in upstream.content:
                    line = line.rstrip(b'\r\n')
                    if not line:
                        if pending:
                            data = b'\n'.join(pending); pending.clear()
                            if data != b'[DONE]':
                                event = json.loads(data)
                                observe_event(event)
                                await translator.event(event)
                        continue
                    if line.startswith(b'data:'):
                        pending.append(line[5:].lstrip(b' '))
                if pending and b'\n'.join(pending) != b'[DONE]':
                    event = json.loads(b'\n'.join(pending))
                    observe_event(event)
                    await translator.event(event)
                if translator.final is None:
                    raise RuntimeError('ChatGPT stream ended without a completed response. Please retry.')
                raw_usage = getattr(translator, 'raw_usage', {})
                details = raw_usage.get('input_tokens_details') or {}
                self.audit('completed', request=request_id, model=model,
                    **response_info,
                    response=getattr(translator, 'response_id', None),
                    input_tokens=raw_usage.get('input_tokens', 0),
                    cached_tokens=details.get('cached_tokens', 0),
                    cached_tokens_reported='cached_tokens' in details,
                    cache_write_tokens=details.get('cache_write_tokens', 0),
                    cache_write_tokens_reported='cache_write_tokens' in details,
                    output_tokens=raw_usage.get('output_tokens', 0),
                    reasoning_tokens=(raw_usage.get('output_tokens_details') or {}).get('reasoning_tokens', 0))
                if trace_key:
                    # Bounded, process-local hashes only. Failed/cancelled
                    # requests must not become successful-prefix evidence.
                    self.last_prefixes.pop(trace_key, None)
                    self.last_prefixes[trace_key] = trace
                    while len(self.last_prefixes) > 128:
                        self.last_prefixes.pop(next(iter(self.last_prefixes)))
                # Keep only bounded in-memory call records. Nothing is added
                # back unless the client still supplies that exact visible
                # call under this conversation, agent, request class and model.
                if model == self.aliases.get(payload.get('model')):
                    self.remember_function_calls(replay_scope, translator.final.get('content', []), model)
                if category in ('main', 'compaction'):
                    self.remember_cache(session_id, converted, details)
                request['bridge_completed'] = getattr(translator, 'completed', False)
                if request['bridge_completed'] and self.credential_kind(request) == 'desktop':
                    from .desktop import observe_completion
                    observe_completion(self.directory, payload, alias, translator.final.get('content', []))
            except ResponseError as error:
                self.audit('upstream_error', request=request_id, model=model, status=error.status, code=error.code,
                    **response_info)
                if error.code == QUOTA_CODE:
                    raise QuotaWait(upstream.headers, safe=not output_observed)
                if not stream:
                    return error_response(
                        error.status, str(error), error.kind, code=error.code,
                        retry=False if error.status in (400, 401, 403) else None)
                await emit('error', {'error': {'type': error.kind, 'message': str(error), 'code': error.code}})
            except (RuntimeError, ValueError, KeyError, aiohttp.ClientError) as error:
                self.audit('transport_error', request=request_id, model=model, error_type=type(error).__name__)
                if not stream:
                    return error_response(502, str(error))
                await emit('error', {'error': {'type': 'api_error', 'message': str(error)}})
            except ConnectionResetError:
                self.audit('client_disconnected', request=request_id, model=model)
            if stream:
                return reply.response
            return web.json_response(translator.final, headers={'X-Local-Model-Route': 'chatgpt-subscription'})

    def app(self):
        @web.middleware
        async def cancellation_audit(request, handler):
            try:
                return await handler(request)
            except asyncio.CancelledError:
                if request.get('bridge_request_id'):
                    self.audit('cancelled', request=request['bridge_request_id'],
                               model=request['bridge_model'])
                raise
        app = web.Application(client_max_size=64 * 1024**2,
                              middlewares=[cancellation_audit])
        app.cleanup_ctx.append(self.setup)
        app.router.add_get('/health', self.health)
        app.router.add_route('*', '/{tail:.*}', self.handle)
        return app
