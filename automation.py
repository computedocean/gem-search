"""Durable, bounded launch queue. Default mode has no financial side effects."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
import zlib
from launch_provider import configured_provider, bridge_missing, run_bridge

CAP = 25_000_000
DAILY_COUNT = 5
WINDOW = 86400
JOB_ID = re.compile(r'[a-f0-9]{24}')


def launch_mode():
    """Fail closed: only the exact value 'live' enables live mode, and a deployment lock overrides it."""
    if os.getenv('GEM_SEARCH_DRY_RUN_LOCK') == '1':
        return 'dry_run'
    return 'live' if os.getenv('LAUNCH_MODE') == 'live' else 'dry_run'


def valid_draft(draft, mode):
    """Shape check at the execution boundary; drafts are built locally but read back from storage."""
    return (isinstance(draft, dict) and isinstance(draft.get('id'), str) and JOB_ID.fullmatch(draft['id']) is not None
            and draft.get('mode') == mode and draft.get('amount') == 0
            and isinstance(draft.get('name'), str) and 0 < len(draft['name']) <= 32
            and isinstance(draft.get('symbol'), str) and re.fullmatch('[A-Z0-9]{1,10}', draft['symbol']) is not None
            and isinstance(draft.get('description'), str) and isinstance(draft.get('image_base64'), str)
            and isinstance(draft.get('provider'), dict))


def load_env(root):
    path = root / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if sep and re.fullmatch('[A-Z][A-Z0-9_]*', key.strip()):
                os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def token_icon(seed):
    """Small deterministic geometric PNG; no paid image-generation service."""
    digest = hashlib.sha256(seed.encode()).digest()
    size = 256
    rows = []
    color = (100 + digest[0] % 150, 100 + digest[1] % 150, 100 + digest[2] % 150)
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            diamond = abs(x - 128) + abs(y - 128) < 90
            cut = (x // 32 + y // 32 + digest[3]) % 3 == 0
            row.extend(color if diamond and not cut else (19, 24, 16))
        rows.append(row)
    def chunk(kind, body):
        return struct.pack('!I', len(body)) + kind + body + struct.pack('!I', zlib.crc32(kind + body))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', size, size, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b'')


class Automation:
    def __init__(self, root, data, connect, event):
        self.root, self.data, self.connect, self.event = root, data, connect, event
        self.enabled = not (data / 'launch-paused').exists()
        self.error = None
        with connect() as con:
            con.executescript('''
            CREATE TABLE IF NOT EXISTS launch_jobs (
              id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, mode TEXT, created REAL,
              reserved REAL, status TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS narrative_items (
              id TEXT PRIMARY KEY, topic TEXT, created REAL, payload TEXT);
            CREATE TABLE IF NOT EXISTS service_cache (key TEXT PRIMARY KEY, updated REAL, payload TEXT);
            ''')

    def mode(self):
        return launch_mode()

    def provider_status(self):
        try:
            provider = configured_provider()
            return {k: v for k, v in provider.items() if k != 'endpoint'}
        except ValueError as error:
            return {'kind': 'invalid', 'label': 'Not configured', 'error': str(error)}

    def missing(self):
        try:
            provider = configured_provider()
        except ValueError as error:
            return [str(error)]
        if provider['kind'] == 'bridge':
            return bridge_missing()
        missing = []
        if not os.getenv('PINATA_JWT'):
            missing.append('PINATA_JWT')
        wallet = Path(os.getenv('SOLANA_KEYPAIR_PATH', str(self.data / 'wallets/treasury.json')))
        if not wallet.is_file():
            missing.append('treasury wallet')
        if not (self.root / 'node_modules/@solana/web3.js').exists():
            missing.append('npm install')
        return missing

    def enqueue(self, project):
        # Tickers, contract addresses and emerging phrases belong to other people: research only.
        if project['source'] == 'demo' or project['status'] != 'shortlisted' or project.get('research_only'):
            return False
        updated = project.get('observed_at', time.time())
        if time.time() - updated > WINDOW or updated > time.time() + 300:
            return False
        # One real concept per project/topic, permanently deduplicated, across restarts.
        topic_key = project.get('topic')
        evidence_ids = ','.join(sorted(str(p.get('id', '')) for p in project.get('posts', [])))
        basis = topic_key + ':' + evidence_ids if topic_key else project['url']
        fingerprint = hashlib.sha256(basis.encode()).hexdigest()
        mode = self.mode()
        try:
            provider = configured_provider()
        except ValueError:
            return False
        identity = hashlib.sha256((mode + fingerprint).encode()).hexdigest()[:24]
        suffix = fingerprint[:5].upper()
        topic = project.get('topic', 'signal')
        names = {'agents': 'Orbit', 'robotics': 'Mech', 'privacy': 'Veil', 'devtools': 'Forge', 'science': 'Nova'}
        name = f"{names.get(topic, 'Signal')} Bloom {suffix}"
        draft = {'id': identity, 'name': name[:32], 'symbol': ('G' + suffix)[:10],
                 'description': f'Independent experimental community token inspired by the {topic} narrative. No affiliation with referenced projects. No promise of returns.',
                 'narrative': project['name'], 'source_url': project['url'], 'source': project['source'],
                 'topic': topic_key,
                 'created': time.time(), 'status': 'queued', 'amount': 0, 'priority_fee': .00001,
                 'provider': provider, 'mode': mode, 'cap_sol': .025, 'image_base64': base64.b64encode(token_icon(identity)).decode()}
        with self.connect() as con:
            # Topic check and insert share one write transaction, so concurrent scans cannot both pass the check.
            con.execute('BEGIN IMMEDIATE')
            if topic_key:
                recent = con.execute('SELECT payload FROM launch_jobs WHERE mode=? AND created>?', (mode, time.time() - 7 * WINDOW))
                if any(json.loads(r['payload']).get('topic') == topic_key for r in recent):
                    return False
            result = con.execute('INSERT OR IGNORE INTO launch_jobs VALUES (?,?,?,?,?,?,?)',
                                 (identity, mode + ':' + fingerprint, mode, time.time(), None, 'queued', json.dumps(draft)))
        if result.rowcount:
            self.event(f"AUTO {mode}: queued {draft['name']}")
        return bool(result.rowcount)

    def status(self):
        with self.connect() as con:
            jobs = []
            for row in con.execute('SELECT * FROM launch_jobs ORDER BY created DESC LIMIT 100'):
                item = json.loads(row['payload'])
                item.pop('image_base64', None)
                if 'provider' in item:
                    item['provider'] = {k: v for k, v in item['provider'].items() if k != 'endpoint'}
                item['status'] = row['status']
                jobs.append(item)
            used = con.execute('SELECT COUNT(*) FROM launch_jobs WHERE mode=? AND reserved>?', (self.mode(), time.time() - WINDOW)).fetchone()[0]
        return {'provider': self.provider_status(), 'enabled': self.enabled, 'mode': self.mode(), 'max_sol': .025, 'daily_max': 5, 'used': used,
                'reserved_sol': round(used * .025, 3), 'daily_sol': .125, 'window': 'rolling_24h',
                'missing': self.missing(), 'error': self.error, 'jobs': jobs}

    def tick(self):
        if not self.enabled:
            return
        # Cross-process mutex; OS releases it after crashes. Never two active signers.
        with open(self.data / 'launcher.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                self._tick()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _tick(self):
        mode = self.mode()
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            # Reconcile previous attempt first. Unknown outcome blocks all new allocations.
            row = con.execute("SELECT * FROM launch_jobs WHERE mode=? AND status IN ('running','pending') ORDER BY reserved LIMIT 1", (mode,)).fetchone()
            if not row:
                if mode == 'live' and self.missing():
                    return
                used = con.execute('SELECT COUNT(*) FROM launch_jobs WHERE mode=? AND reserved>?', (mode, time.time() - WINDOW)).fetchone()[0]
                if used >= DAILY_COUNT:
                    return
                con.execute("UPDATE launch_jobs SET status='expired' WHERE mode=? AND status='queued' AND created<?", (mode, time.time() - WINDOW))
                row = con.execute("SELECT * FROM launch_jobs WHERE mode=? AND status='queued' ORDER BY created LIMIT 1", (mode,)).fetchone()
                if not row:
                    return
                con.execute("UPDATE launch_jobs SET reserved=?,status='running' WHERE id=?", (time.time(), row['id']))
            draft = json.loads(row['payload'])
        if mode == 'dry_run':
            result = {'status': 'dry_run_complete', 'reason': 'Offline rehearsal. No metadata upload, transaction, SOL cost or on-chain simulation.'}
        elif not valid_draft(draft, mode) or row['mode'] != mode:
            result = {'status': 'blocked', 'reason': 'Job failed validation at the execution boundary; no provider was called.'}
        elif launch_mode() != 'live' or self.missing():
            # Re-read at the boundary: configuration can change between reservation and execution.
            result = {'status': 'pending', 'reason': 'Live configuration unavailable at execution time; no provider was called.'}
        elif draft.get('provider', {}).get('kind') == 'bridge':
            def persist(value):
                with self.connect() as con:
                    con.execute('UPDATE launch_jobs SET payload=? WHERE id=?', (json.dumps(value), value['id']))
            result = run_bridge(draft, persist)
        elif draft.get('provider', {'kind': 'pumpportal'}).get('kind') != 'pumpportal':
            result = {'status': 'pending', 'reason': 'Unknown pinned launch provider; execution stopped.'}
        else:
            try:
                proc = subprocess.run(['node', str(self.root / 'launch/worker.mjs'), 'run'], input=json.dumps(draft),
                                      text=True, capture_output=True, timeout=150, cwd=self.root)
                result = json.loads(proc.stdout.strip().splitlines()[-1])
                if result.get('status') == 'worker_error':
                    jobfile = self.data / 'launch-jobs' / draft['id'] / 'state.json'
                    state = json.loads(jobfile.read_text()) if jobfile.exists() else {}
                    # Never continue to another launch after uncertainty about a signed transaction.
                    result['status'] = 'pending' if state.get('funding') else 'blocked'
            except (subprocess.TimeoutExpired, ValueError, IndexError, OSError):
                result = {'status': 'pending', 'reason': 'Worker interrupted. Reconcile same job before any new allocation.'}
        draft.update(result)
        with self.connect() as con:
            con.execute('UPDATE launch_jobs SET status=?,payload=? WHERE id=?', (result['status'], json.dumps(draft), row['id']))
        if row['status'] != result['status']:
            self.event(f"AUTO {mode}: {draft['name']} → {result['status']}")


DEFAULT_TOPICS = {
    'agents': r'\b(agent[s]?|agentic|llm|language model|artificial intelligence|агент\w*|нейросет\w*|искусственн\w+ интеллект\w*)\b',
    'robotics': r'\b(robot[s]?|robotics|humanoid|drone[s]?|робот\w*|дрон\w*)\b',
    'privacy': r'\b(privacy|encryption|zero.knowledge|cryptography|приватност\w*|шифрован\w*|криптограф\w*)\b',
    'devtools': r'\b(compiler|debugger|developer tool|database|open.source|компилятор\w*|отладчик\w*|открыт\w+ код\w*)\b',
    'science': r'\b(fusion|quantum|telescope|spacecraft|satellite|квантов\w*|телескоп\w*|спутник\w*)\b',
}


def load_topics(path):
    """Local topics.json replaces the built-in taxonomy; an invalid file never breaks the engine."""
    if not path.is_file():
        return dict(DEFAULT_TOPICS)
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not 1 <= len(data) <= 40:
            raise ValueError('expected an object with 1-40 topics')
        topics = {}
        for name, regex in data.items():
            if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,23}', name):
                raise ValueError(f'invalid topic name {name!r}')
            if not isinstance(regex, str) or not 1 <= len(regex) <= 600:
                raise ValueError(f'invalid pattern for {name}')
            re.compile(regex, re.I)
            topics[name] = regex
        return topics
    except (ValueError, re.error) as exc:
        print(f'topics.json ignored, using built-in topics: {exc}', file=sys.stderr)
        return dict(DEFAULT_TOPICS)


TOPICS = load_topics(Path(__file__).resolve().parent / 'topics.json')


def discover_free(engine, fetch):
    """Official HN API, top 30 stories. Not a substitute for an X firehose."""
    with engine.connect() as con:
        previous = con.execute("SELECT updated FROM service_cache WHERE key='hn_poll'").fetchone()
        if previous and time.time() - previous[0] < 1800:
            return []
        # Record attempt as well as success so errors never create an API loop.
        con.execute("INSERT OR REPLACE INTO service_cache VALUES ('hn_poll',?,'{}')", (time.time(),))
    ids = json.loads(fetch('https://hacker-news.firebaseio.com/v0/topstories.json')[1])[:30]
    items = []
    failed = 0
    for identity in ids:
        if type(identity) is not int:
            continue
        try:
            item = json.loads(fetch(f'https://hacker-news.firebaseio.com/v0/item/{identity}.json')[1])
            if not item or item.get('deleted') or item.get('dead') or item.get('type') != 'story':
                continue
            if time.time() - item.get('time', 0) > WINDOW or item.get('score', 0) < 20:
                continue
            from urllib.parse import urlsplit
            url = item.get('url', '')
            parsed = urlsplit(url)
            if parsed.scheme not in ('https', 'http') or not parsed.hostname:
                continue
            for topic, regex in TOPICS.items():
                if re.search(regex, item.get('title', ''), re.I):
                    record = {'id': str(identity), 'title': item['title'], 'url': url, 'domain': parsed.hostname,
                              'score': item['score'], 'comments': item.get('descendants', 0), 'time': item['time'],
                              'author': item.get('by', 'unknown'), 'topic': topic}
                    items.append(record)
                    with engine.connect() as con:
                        con.execute('INSERT OR REPLACE INTO narrative_items VALUES (?,?,?,?)', (str(identity), topic, item['time'], json.dumps(record)))
                    break
        except Exception:
            failed += 1
            continue
    with engine.connect() as con:
        con.execute('DELETE FROM narrative_items WHERE created<?', (time.time() - WINDOW,))
        rows = [json.loads(row['payload']) for row in con.execute('SELECT payload FROM narrative_items')]
    projects = []
    for topic in TOPICS:
        matches = [i for i in rows if i['topic'] == topic]
        if not matches:
            continue
        domains = {i['domain'] for i in matches}
        authors = {i['author'] for i in matches}
        score = sum(i['score'] for i in matches)
        comments = sum(i['comments'] for i in matches)
        # Story scores are not treated as unique users. Four explicit narrative checks.
        checks = [('lookout', len(matches) >= 3, f'{len(matches)} related stories in 24h; HN attention, not X organic growth.'),
                  ('maker', len(domains) >= 3, f'{len(domains)} distinct linked domains. Narrative evidence, not product verification.'),
                  ('skeptic', len(authors) >= 3, f'{len(authors)} submitting accounts. Account authenticity not verified.'),
                  ('runner', score >= 100 and comments >= 20, f'{score} HN points and {comments} comments. No revenue or return forecast.')]
        votes = [{'seat': s, 'vote': 'pass' if ok else 'hold', 'reason': why} for s, ok, why in checks]
        first = max(matches, key=lambda i: i['score'])
        projects.append({'id': 'hn-' + topic, 'name': topic.title() + ' / HN narrative', 'url': first['url'], 'source': 'hn',
                         'topic': topic, 'observed_at': max(i['time'] for i in matches), 'status': 'shortlisted' if all(c[1] for c in checks) else 'held',
                         'score': sum(c[1] for c in checks) * 25, 'votes': votes,
                         'signals': {'mentions': len(matches), 'authors': len(authors), 'growth': None, 'points': score, 'comments': comments},
                         'evidence': {'synthetic': False, 'errors': [], 'pages': [{'url': i['url'], 'kind': 'HN story', 'text': i['title'], 'fetched_at': time.time()} for i in matches]},
                         'posts': matches})
    engine.event(f'FREE HN: checked {len(ids)} stories; {len(projects)} narrative groups; {failed} fetch/parse errors')
    return projects
