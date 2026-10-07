"""Account model discovery and local usage totals."""
import json
from pathlib import Path
import aiohttp
from .auth import Auth, RESOURCE, atomic_json


async def refresh_models(directory):
    auth = Auth(directory)
    async with aiohttp.ClientSession() as session:
        headers = await auth.headers(session)
        async with session.get(RESOURCE + '/models', headers=headers, allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                raise RuntimeError(f'ChatGPT model list failed (HTTP {response.status}).')
            catalog = await response.json()
        # Refreshing a picker must never generate billable inference, including
        # while the quota guard is active. Retain previously verified additions
        # when a catalog lags them, without re-testing the model automatically.
        path = Path(directory) / 'models.json'
        previous = json.loads(path.read_text()) if path.exists() else []
        listed = {m.get('slug') for m in catalog.get('models', [])}
        supplemental = [m for m in previous if m.get('verified_direct_inference') and m.get('slug') not in listed]
    models = [m for m in catalog['models'] if m.get('visibility') == 'list']
    models = supplemental + models
    if not models or not all(isinstance(m.get('slug'), str) and isinstance(m.get('display_name'), str) for m in models):
        raise RuntimeError('ChatGPT returned an empty or invalid account model catalog.')
    atomic_json(Path(directory) / 'models.json', models)
    return [{'model': 'chatgpt.' + m['slug'], 'label': m['display_name'] + ' · ChatGPT subscription',
             'description': 'Uses the ChatGPT account connected to this local adapter.'} for m in models]


def usage_report(directory):
    """Read the retained private ledger; no auth refresh or network calls."""
    directory = Path(directory)
    rows = []
    malformed = 0
    for path in [directory / 'usage-audit.jsonl.2', directory / 'usage-audit.jsonl.1', directory / 'usage-audit.jsonl']:
        if not path.exists(): continue
        for line in path.read_text().splitlines():
            try: rows.append(json.loads(line))
            except ValueError: malformed += 1
    starts = {r['request']: r for r in rows if r.get('event') == 'upstream_started'}
    seen, groups = set(), {}
    for row in rows:
        if row.get('event') != 'completed': continue
        identity = row.get('response') or row.get('request')
        if not identity or identity in seen: continue
        seen.add(identity)
        start = starts.get(row.get('request'), {})
        key = (row.get('model', 'unknown'), start.get('request_class', 'unknown'), start.get('routing', 'older_adapter'))
        group = groups.setdefault(key, dict(zip(('model', 'request_class', 'routing'), key)))
        for field, value in {'requests': 1, **{k: row.get(k, 0) for k in
                ('input_tokens', 'cached_tokens', 'cache_write_tokens', 'output_tokens', 'reasoning_tokens')}}.items():
            group[field] = group.get(field, 0) + value
    return {'scope': 'Retained local adapter logs only; not the account billing statement.',
            'quota_paused': (directory / 'quota-paused.json').exists(),
            'first_event_time': min((r.get('time', 0) for r in rows), default=None),
            'last_event_time': max((r.get('time', 0) for r in rows), default=None),
            'groups': list(groups.values()),
            'blocked_locally': sum(r.get('event') == 'blocked_locally' for r in rows),
            'upstream_errors': sum(r.get('event') in ('upstream_error', 'transport_error') for r in rows),
            'malformed_log_lines': malformed}
