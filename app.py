#!/usr/bin/env python3
"""Gem Search: local, evidence-first project research workbench."""
import argparse
from contextlib import contextmanager
import concurrent.futures
import hashlib
import hmac
import http.client
from http.cookies import CookieError, SimpleCookie
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urljoin, urlsplit, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from automation import Automation, discover_free, load_env
from grok import GrokReview
from jev import Jev

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DB = DATA / 'gem-search.sqlite'
SCAN_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
STATE = {'running': False, 'stage': 'idle', 'error': None, 'autopilot': False,
         'interval': 300, 'last_poll': None}
STOP = threading.Event()
MAX_BODY = 2_000_000
AUTO = None
JEV = None
GROK = None
EXTENSION_KEY = ''
# Dashboard sign-in secret, persisted in data/dashboard-token. Never served over HTTP.
DASHBOARD_TOKEN = ''
SESSION_COOKIE = 'gem_session'
SECRET_FORMAT = re.compile(r'[A-Za-z0-9_-]{40,100}')
RETENTION_DAYS = 30
LOGIN_PAGE = (ROOT / 'static' / 'login.html').read_bytes()


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def ensure_secret(path):
    """Read a local secret, creating it atomically (0600) if absent or truncated."""
    try:
        value = path.read_text().strip()
        if SECRET_FORMAT.fullmatch(value):
            return value
    except FileNotFoundError:
        pass
    value = secrets.token_urlsafe(32)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return value


def derived(purpose):
    """Stable per-install values derived from the sign-in secret; empty when unset."""
    if not DASHBOARD_TOKEN:
        return ''
    return hmac.new(DASHBOARD_TOKEN.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def login_url(port):
    return f'http://127.0.0.1:{port}/login?token={DASHBOARD_TOKEN}'


def prune_files(retention_days=None):
    """Remove old regular files from processed/ and failed/ only. Never inbox or job state."""
    days = RETENTION_DAYS if retention_days is None else retention_days
    cutoff = time.time() - days * 86400
    removed = 0
    for folder in ('processed', 'failed'):
        directory = DATA / folder
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def init():
    DATA.mkdir(exist_ok=True)
    (DATA / 'inbox').mkdir(exist_ok=True)
    (DATA / 'processed').mkdir(exist_ok=True)
    (DATA / 'failed').mkdir(exist_ok=True)
    with connect() as con:
        con.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY, created TEXT, source TEXT, posts INTEGER,
          candidates INTEGER, shortlisted INTEGER, held INTEGER, rejected INTEGER);
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY, name TEXT, url TEXT, source TEXT,
          status TEXT, score INTEGER, updated TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, created TEXT, message TEXT);
        CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS launches (id TEXT PRIMARY KEY, created TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, source TEXT, url TEXT, timestamp REAL, payload TEXT);
        CREATE TABLE IF NOT EXISTS service_cache (key TEXT PRIMARY KEY, updated REAL, payload TEXT);
        ''')


def event(message):
    with connect() as con:
        con.execute('INSERT INTO events(created,message) VALUES (?,?)', (now(), message))
        con.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 100)')


def stage(value):
    with STATE_LOCK:
        STATE['stage'] = value


def web_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('URL must be a string of at most 2048 characters')
    p = urlsplit(value)
    if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password:
        raise ValueError('Only public HTTP(S) URLs without credentials are allowed')
    if p.port not in (None, 80, 443):
        raise ValueError('Only standard web ports are allowed')
    return p


def fetch(url):
    """Resolve, validate all addresses, and pin connection to a public IP."""
    deadline = time.monotonic() + 20
    for _ in range(4):
        p = web_url(url)
        host = p.hostname.encode('idna').decode('ascii')
        port = p.port or (443 if p.scheme == 'https' else 80)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('Private, local and reserved addresses are blocked')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Fetch deadline exceeded')
        raw = socket.create_connection((addresses[0][4][0], port), timeout=min(6, remaining))
        conn = http.client.HTTPConnection(host, port, timeout=6)
        try:
            conn.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if p.scheme == 'https' else raw
            path = p.path or '/'
            if p.query:
                path += '?' + p.query
            conn.request('GET', path, headers={'Host': p.netloc, 'User-Agent': 'GemSearch/0.1 (public project research)', 'Accept': 'text/html,text/plain,application/json'})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                target = response.getheader('Location')
                if not target:
                    raise ValueError('Redirect has no location')
                url = urljoin(url, target)
                continue
            if response.status != 200:
                raise ValueError(f'HTTP {response.status}')
            content_type = response.getheader('Content-Type', '').lower()
            if not any(t in content_type for t in ('text/html', 'text/plain', 'application/json')):
                raise ValueError('Unsupported content type')
            chunks, size = [], 0
            while size <= 600_000:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Fetch deadline exceeded')
                if conn.sock:
                    conn.sock.settimeout(min(6, remaining))
                chunk = response.read1(min(16384, 600_001 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > 600_000:
                raise ValueError('Page exceeds 600 KB limit')
            return url, b''.join(chunks).decode('utf-8', errors='replace')
        finally:
            conn.close()
            raw.close()
    raise ValueError('Too many redirects')


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.parts, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.skip += 1
        if tag == 'a':
            href = dict(attrs).get('href')
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def crawl(url):
    pages, errors, queue, visited = [], [], [url], set()
    while queue and len(visited) < 3:
        target = queue.pop(0)
        if target in visited:
            continue
        visited.add(target)
        try:
            final, body = fetch(target)
            page = Page()
            page.feed(body)
            text = re.sub(r'\s+', ' ', ' '.join(page.parts)).strip()[:16000]
            host = urlsplit(final).hostname
            path = urlsplit(final).path.strip('/')
            kind = 'repository' if host == 'github.com' and len(path.split('/')) >= 2 else ('docs' if 'docs' in final.lower() else 'website')
            pages.append({'url': final, 'kind': kind, 'text': text, 'fetched_at': now()})
            for href in page.links:
                link = urljoin(final, href).split('#')[0]
                try:
                    parsed = web_url(link)
                except (ValueError, TypeError):
                    continue
                if parsed.hostname == 'github.com' or (parsed.hostname == host and any(w in parsed.path.lower() for w in ('docs', 'about', 'whitepaper'))):
                    if link not in visited and link not in queue:
                        queue.append(link)
            queue = queue[:8]
        except Exception as exc:
            errors.append({'url': target, 'error': str(exc)[:200]})
    return {'pages': pages, 'errors': errors, 'synthetic': False}


def validate_posts(data):
    posts = data.get('posts') if isinstance(data, dict) else data
    if not isinstance(posts, list) or not 1 <= len(posts) <= 1000:
        raise ValueError('Expected 1–1000 posts in a JSON array or {"posts": [...]}')
    result = []
    for i, post in enumerate(posts):
        if not isinstance(post, dict):
            raise ValueError(f'Post {i + 1} must be an object')
        for key in ('id', 'author', 'text', 'project_url', 'created_at'):
            if not isinstance(post.get(key), str) or not post[key].strip():
                raise ValueError(f'Post {i + 1}: missing string field {key}')
        if any(len(post[key]) > limit for key, limit in [('id', 300), ('author', 300), ('text', 10000)]):
            raise ValueError(f'Post {i + 1}: field too long')
        web_url(post['project_url'])
        try:
            timestamp = datetime.fromisoformat(post['created_at'].replace('Z', '+00:00'))
            if timestamp.tzinfo is None:
                raise ValueError()
        except ValueError:
            raise ValueError(f'Post {i + 1}: created_at must be an ISO timestamp with timezone')
        followers = post.get('followers')
        if followers is not None and (type(followers) is not int or followers < 0):
            raise ValueError(f'Post {i + 1}: followers must be a nonnegative integer')
        p = urlsplit(post['project_url'])
        clean = dict(post)
        clean['project_url'] = f'{p.scheme}://{p.netloc.lower()}{p.path.rstrip("/")}'
        clean['project_name'] = str(post.get('project_name') or p.hostname)[:100]
        clean['timestamp'] = timestamp.timestamp()
        result.append(clean)
    return result


def cached_crawl(url):
    key='crawl:'+hashlib.sha256(url.encode()).hexdigest()
    with connect() as con:
        row=con.execute('SELECT payload FROM service_cache WHERE key=? AND updated>?',(key,time.time()-1800)).fetchone()
    if row:
        return json.loads(row[0])
    result=crawl(url)
    with connect() as con:
        con.execute('INSERT OR REPLACE INTO service_cache VALUES (?,?,?)',(key,time.time(),json.dumps(result)))
        # Expired crawl entries are never read again; keep the volume bounded.
        con.execute("DELETE FROM service_cache WHERE key LIKE 'crawl:%' AND updated<?",(time.time()-86400,))
    return result


def apply_grok(project):
    if not GROK or project['source']=='demo' or project['signals'].get('authors',0)<4:
        return project
    project['grok']=GROK.review(project)
    config=GROK.status()
    # A missing key, exhausted budget, timeout or malformed model reply is not
    # evidence against a candidate. Only a complete four-seat review may change
    # the local evidence verdict; preserve a previous verdict on unavailable review.
    if not config['enabled'] or not config['configured'] or project['grok'].get('status')!='complete':
        return project
    votes=project['grok']['votes']
    if len(votes)!=4 or not all(v.get('available') for v in votes):
        return project
    if any(v['vote']=='reject' for v in votes):
        project['status']='rejected'
    elif not all(v['vote']=='pass' for v in votes):
        if project['status']!='rejected':project['status']='held'
    return project


def save_projects(projects):
    with connect() as con:
        for result in projects:
            con.execute('INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?,?,?)',
                        tuple(result[k] for k in ('id','name','url','source','status','score','updated'))+(json.dumps(result),))


def spider_loop():
    while not STOP.wait(10):
        posts=JEV.pending()
        if not posts:continue
        if not SCAN_LOCK.acquire(blocking=False):continue
        owned=True
        with STATE_LOCK:STATE.update(running=True,stage='JEV · browser capture',error=None)
        try:
            # Project links get the normal crawler pipeline; at most one link per post.
            linked=[dict(p,project_url=p['links'][0],project_name=urlsplit(p['links'][0]).hostname) for p in posts if p['links']]
            if linked:
                # scan owns/releases the lock; reacquire it for narrative synthesis.
                validated=validate_posts(linked)
                owned=False
                ok=scan(validated,'spider')
                if not ok:continue
                owned=SCAN_LOCK.acquire(blocking=False)
                if not owned:continue
                with STATE_LOCK:STATE.update(running=True,stage='JEV · narratives')
            projects=[apply_grok(p) for p in JEV.narratives()]
            save_projects(projects)
            if AUTO and os.getenv('SPIDER_ALLOW_LAUNCH')=='1':
                for p in projects:AUTO.enqueue(p)
            JEV.ack(posts)
            event(f'SPIDER: processed {len(posts)} visible posts; {len(projects)} narrative groups')
        except Exception as exc:
            event('Spider analysis failed: '+type(exc).__name__)
        finally:
            if owned:
                with STATE_LOCK:STATE.update(running=False,stage='idle')
                SCAN_LOCK.release()


def signals(posts):
    authors = {p['author'].lower() for p in posts}
    texts = [re.sub(r'https?://\S+|[^\w\s]', '', p['text'].lower()).strip() for p in posts]
    duplicate_ratio = 1 - len(set(texts)) / len(posts)
    followers = {p['author'].lower(): p['followers'] for p in posts if p.get('followers') is not None}
    end = max(p['timestamp'] for p in posts)
    recent = sum(end - p['timestamp'] < 3600 for p in posts)
    previous = sum(3600 <= end - p['timestamp'] < 7200 for p in posts)
    growth = round(recent / previous, 1) if previous else None
    risk_terms = sorted({term for term in ('seed phrase', 'private key', 'guaranteed profit', 'guaranteed 100x', 'connect wallet to claim') if any(term in p['text'].lower() for p in posts)})
    return {'mentions': len(posts), 'authors': len(authors), 'duplicate_ratio': round(duplicate_ratio, 2),
            'known_audiences': len(followers), 'authors_over_100': sum(v >= 100 for v in followers.values()),
            'recent': recent, 'previous': previous, 'growth': growth, 'risk_terms': risk_terms,
            'window_end': datetime.fromtimestamp(end, timezone.utc).isoformat()}


def vote(seat, sig, evidence):
    pages = evidence['pages']
    text = ' '.join(p['text'].lower() for p in pages)
    if seat == 'lookout':
        ok = sig['authors'] >= 4 and sig['duplicate_ratio'] <= .35 and sig['recent'] >= 4
        return {'seat': seat, 'vote': 'pass' if ok else 'hold', 'reason': f"{sig['authors']} unique authors; {sig['duplicate_ratio']:.0%} repeated text; {sig['recent']} mentions in the latest observed hour. Organic growth is not proven."}
    if seat == 'maker':
        repo = any(p['kind'] == 'repository' and len(p['text']) >= 100 for p in pages)
        docs = any(p['kind'] == 'docs' and len(p['text']) >= 100 for p in pages)
        ok = repo and docs
        return {'seat': seat, 'vote': 'pass' if ok else 'hold', 'reason': f"Repository page {'found' if repo else 'not confirmed'}; documentation {'found' if docs else 'not confirmed'}. Code quality and working product are not verified."}
    if seat == 'skeptic':
        risks = sorted(set(sig['risk_terms']) | {term for term in ('seed phrase', 'guaranteed profit', 'connect wallet to claim') if term in text})
        rejected = bool(risks) or sig['duplicate_ratio'] > .65
        return {'seat': seat, 'vote': 'reject' if rejected else ('pass' if pages else 'hold'), 'reason': ('Risk keywords: ' + ', '.join(risks) if risks else 'High repeated-text ratio' if rejected else 'No configured red flags in collected text; this is not a security audit.')}
    website = any(p['kind'] == 'website' and len(p['text']) >= 100 for p in pages)
    return {'seat': seat, 'vote': 'pass' if website and not evidence['errors'] else 'hold', 'reason': f"{len(pages)} public pages collected; {len(evidence['errors'])} fetch errors. Team identity, contracts and liquidity remain unverified."}


def demo_posts():
    """Entirely fictional fixture, not a market observation."""
    specs = [('Orbit Agents', 'orbit-agents.example', 12, False),
             ('Signal Garden', 'signal-garden.example', 7, False),
             ('Moon Claim', 'moon-claim.example', 14, True),
             ('Tiny Protocol', 'tiny-protocol.example', 2, False)]
    result = []
    for name, domain, count, spam in specs:
        for i in range(count):
            stamp = time.time() - (3900 if i == 0 else i * 130)
            result.append({'id': f'{domain}-{i}', 'author': f'researcher-{i}' if not spam else f'promo-{i % 3}',
                           'text': 'Guaranteed profit. Connect wallet to claim' if spam else f'{name}: research note {i}, testing component {i * 7} and public release {i + 1}.',
                           'project_name': name, 'project_url': f'https://{domain}', 'followers': 150 + i * 47,
                           'created_at': datetime.fromtimestamp(stamp, timezone.utc).isoformat()})
    return validate_posts(result)


def demo_evidence(url):
    kinds = ['website', 'repository', 'docs'] if 'orbit' in url else ['website']
    return {'synthetic': True, 'errors': [], 'pages': [
        {'url': url + '/' + kind, 'kind': kind, 'fetched_at': now(),
         'text': ('Fictional demo evidence. An experimental open-source agent sandbox with documentation, reproducible experiments and a public development roadmap. ' * 2)} for kind in kinds]}


def read_x():
    bearer = os.getenv('X_BEARER_TOKEN', '')
    if not bearer:
        raise ValueError('Set X_BEARER_TOKEN locally before requesting X data')
    query = os.getenv('X_QUERY', '("open source" OR "AI agents" OR "launched") has:links -is:retweet')
    params = {'query': query, 'max_results': 100, 'tweet.fields': 'created_at,author_id,entities',
              'expansions': 'author_id', 'user.fields': 'username,public_metrics'}
    request = Request('https://api.x.com/2/tweets/search/recent?' + urlencode(params), headers={'Authorization': 'Bearer ' + bearer})
    try:
        with urlopen(request, timeout=20) as response:
            data = json.load(response)
    except HTTPError as exc:
        raise ValueError(f'X API HTTP {exc.code}: check access, credits and rate limits') from None
    if data.get('errors'):
        event('X API returned partial errors; only available posts will be processed')
    users = {u['id']: u for u in data.get('includes', {}).get('users', [])}
    result = []
    for tweet in data.get('data', []):
        user = users.get(tweet.get('author_id'), {})
        for entity in tweet.get('entities', {}).get('urls', []):
            url = entity.get('unwound_url') or entity.get('expanded_url')
            try:
                p = web_url(url)
            except (ValueError, TypeError):
                continue
            if p.hostname in ('x.com', 'twitter.com', 't.co', 'www.x.com', 'www.twitter.com'):
                continue
            result.append({'id': tweet['id'], 'author': user.get('username') or tweet.get('author_id', 'unknown'),
                           'text': tweet['text'], 'project_url': url, 'project_name': p.hostname,
                           'followers': user.get('public_metrics', {}).get('followers_count'),
                           'created_at': tweet['created_at']})
            break
    if not result:
        return []
    # A single bounded recent-search page; never silently describe it as a firehose.
    return validate_posts(result)


def start_x():
    if os.getenv('ENABLE_PAID_X', '0') != '1':
        raise ValueError('Paid X access is disabled in economy mode; set ENABLE_PAID_X=1 explicitly')
    if not os.getenv('X_BEARER_TOKEN'):
        raise ValueError('Set X_BEARER_TOKEN locally and restart the server')
    if not SCAN_LOCK.acquire(blocking=False):
        return False
    with STATE_LOCK:
        STATE.update(running=True, stage='X · recent search', error=None)
    def worker():
        try:
            posts = read_x()
        except Exception as exc:
            with STATE_LOCK:
                STATE.update(running=False, stage='idle', error=str(exc)[:300])
            event('X request failed: ' + str(exc)[:200])
            SCAN_LOCK.release()
            return
        scan(posts, 'x')
    threading.Thread(target=worker, daemon=True).start()
    return True


def create_launch(data):
    if not isinstance(data, dict):
        raise ValueError('Expected an object')
    result = {'id': secrets.token_hex(12), 'created': now(), 'status': 'draft'}
    for key, limit in [('name', 32), ('symbol', 10), ('description', 1000), ('narrative', 500), ('metadata_uri', 2048)]:
        value = data.get(key, '')
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise ValueError(f'{key}: expected text up to {limit} characters')
        result[key] = value.strip()
    if not result['name'] or not result['description'] or not re.fullmatch('[A-Za-z0-9]{1,10}', result['symbol']):
        raise ValueError('Name, description and alphanumeric symbol (1–10 characters) are required')
    result['symbol'] = result['symbol'].upper()
    if result['metadata_uri']:
        web_url(result['metadata_uri'])
    for key, maximum in [('amount', 10), ('priority_fee', .01)]:
        value = data.get(key, 0)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
            raise ValueError(f'{key}: expected a number between 0 and {maximum}')
        result[key] = value
    with connect() as con:
        con.execute('INSERT INTO launches VALUES (?,?,?)', (result['id'], result['created'], json.dumps(result)))
    event(f"Launch draft saved: {result['name']} / {result['symbol']}")
    return result


def check_launch(identity):
    with connect() as con:
        row = con.execute('SELECT payload FROM launches WHERE id=?', (identity,)).fetchone()
        if not row:
            raise ValueError('Launch draft not found')
        result = json.loads(row['payload'])
        result['status'] = 'needs_connection'
        result['result'] = 'Fields checked. The automatic queue launches from the shortlist, separately from manual drafts. This draft was not sent to the network.'
        con.execute('UPDATE launches SET payload=? WHERE id=?', (json.dumps(result), identity))
    return result


def scan(posts, source):
    try:
        stage('JEV · grouping mentions')
        with connect() as con:
            seen = {row['id'] for row in con.execute('SELECT id FROM seen')} if source != 'demo' else set()
        unique, batch = [], set()
        for post in posts:
            identity = hashlib.sha256((source + ':' + post['id']).encode()).hexdigest()
            if identity not in seen and identity not in batch:
                unique.append(post)
                batch.add(identity)
        groups = {}
        for post in unique:
            groups.setdefault(post['project_url'], []).append(post)
        if len(groups) > 30:
            raise ValueError('At most 30 project URLs per scan; split the input into smaller batches')
        if source != 'demo':
            with connect() as con:
                for url in groups:
                    existing = [json.loads(r['payload']) for r in con.execute('SELECT payload FROM observations WHERE source=? AND url=? ORDER BY timestamp DESC LIMIT 2000', (source, url))]
                    combined = {p['id']: p for p in existing + groups[url]}
                    end = max(p['timestamp'] for p in combined.values())
                    groups[url] = [p for p in combined.values() if end - p['timestamp'] < 7200]
        event(f'{source.upper()}: {len(unique)} new posts → {len(groups)} candidates')
        results = []
        for url, mentions in groups.items():
            name = mentions[0]['project_name']
            stage(f'Crawler · {name}')
            sig = signals(mentions)
            evidence = demo_evidence(url) if source == 'demo' else cached_crawl(url)
            stage(f'DOTS · {name}')
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                votes = list(pool.map(lambda seat: vote(seat, sig, evidence), ['lookout', 'maker', 'skeptic', 'runner']))
            status = 'rejected' if any(v['vote'] == 'reject' for v in votes) else ('shortlisted' if all(v['vote'] == 'pass' for v in votes) else 'held')
            score = sum(v['vote'] == 'pass' for v in votes) * 25
            results.append({'id': hashlib.sha256((source + url).encode()).hexdigest()[:20],
                            'name': name, 'url': url, 'source': source, 'status': status, 'score': score,
                            'updated': now(), 'signals': sig, 'evidence': evidence, 'votes': votes,
                            'observed_at': max(p['timestamp'] for p in mentions),
                            'posts': [{k: v for k, v in p.items() if k != 'timestamp'} for p in mentions[:30]]})
            apply_grok(results[-1])
            event(f"{name}: {results[-1]['status']} · {score // 25}/4 local checks passed")
        with connect() as con:
            for result in results:
                con.execute('INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?,?,?)',
                            tuple(result[k] for k in ('id', 'name', 'url', 'source', 'status', 'score', 'updated')) + (json.dumps(result),))
            con.executemany('INSERT OR IGNORE INTO seen VALUES (?)', [(value,) for value in batch] if source != 'demo' else [])
            if source != 'demo':
                for p in unique:
                    identity = hashlib.sha256((source + ':' + p['id']).encode()).hexdigest()
                    con.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?)', (identity, source, p['project_url'], p['timestamp'], json.dumps(p)))
                con.execute('DELETE FROM observations WHERE timestamp < ?', (time.time() - 7 * 86400,))
            con.execute('INSERT INTO runs(created,source,posts,candidates,shortlisted,held,rejected) VALUES (?,?,?,?,?,?,?)',
                        (now(), source, len(unique), len(results), *(sum(r['status'] == status for r in results) for status in ('shortlisted', 'held', 'rejected'))))
        if AUTO and (source!='spider' or os.getenv('SPIDER_ALLOW_LAUNCH')=='1'):
            for result in results:
                AUTO.enqueue(result)
        event('Scan complete. Research shortlist updated.')
        return True
    except Exception as exc:
        with STATE_LOCK:
            STATE['error'] = str(exc)[:300]
        event(f'Scan failed: {str(exc)[:200]}')
        return False
    finally:
        with STATE_LOCK:
            STATE['running'] = False
            STATE['stage'] = 'idle'
        SCAN_LOCK.release()


def begin(posts, source, background=True):
    """None: another scan holds the lock. Otherwise True/started, or the scan result."""
    if not SCAN_LOCK.acquire(blocking=False):
        return None
    with STATE_LOCK:
        STATE.update(running=True, stage='starting', error=None)
    if background:
        threading.Thread(target=scan, args=(posts, source), daemon=True).start()
        return True
    return scan(posts, source)


def autopilot():
    last = 0
    while not STOP.wait(1):
        with STATE_LOCK:
            enabled, interval = STATE['autopilot'], STATE['interval']
        if not enabled or time.monotonic() - last < interval:
            continue
        last = time.monotonic()
        with STATE_LOCK:
            STATE['last_poll'] = now()
        if os.getenv('FREE_FEED_ENABLED', '1') == '1' and AUTO:
            try:
                projects = discover_free(AUTO, fetch)
                with connect() as con:
                    for result in projects:
                        result['updated'] = now()
                        con.execute('INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?,?,?)',
                                    tuple(result[k] for k in ('id', 'name', 'url', 'source', 'status', 'score', 'updated')) + (json.dumps(result),))
                for result in projects:
                    AUTO.enqueue(result)
            except Exception as exc:
                event(f'Free feed unavailable: {type(exc).__name__}; next poll in 30 min')
        if os.getenv('X_BEARER_TOKEN') and os.getenv('ENABLE_PAID_X') == '1':
            start_x()
        process_inbox()
        try:
            prune_files()
        except OSError as exc:
            event(f'Retention cleanup skipped: {type(exc).__name__}')


def process_inbox():
    """Invalid files move to failed/; storage or scan errors leave the file for a retry."""
    for path in sorted((DATA / 'inbox').glob('*.json'))[:10]:
        if path.is_symlink():
            continue
        try:
            if path.stat().st_size > MAX_BODY:
                raise ValueError('Input exceeds 2 MB')
            posts = validate_posts(json.loads(path.read_text()))
        except (ValueError, UnicodeDecodeError) as exc:
            event(f'Inbox {path.name}: {str(exc)[:150]}; moved to failed/')
            try:
                path.rename(DATA / 'failed' / f'{time.time_ns()}-{path.name}')
            except OSError:
                pass
            continue
        except OSError as exc:
            event(f'Inbox {path.name}: unreadable ({type(exc).__name__})')
            continue
        result = begin(posts, 'import', background=False)
        if result is None:
            break
        if not result:
            event(f'Inbox {path.name}: scan failed; kept for retry')
            break
        try:
            path.rename(DATA / 'processed' / f'{time.time_ns()}-{path.name}')
        except OSError as exc:
            # Already scanned; dedup by post id makes the retry harmless.
            event(f'Inbox {path.name}: processed but not archived ({type(exc).__name__})')
            break


def launch_loop():
    while not STOP.wait(15):
        try:
            AUTO.tick()
        except Exception as exc:
            AUTO.error = f'Queue error: {type(exc).__name__}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, status, body, content_type='application/json; charset=utf-8', headers=()):
        payload = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        origin=self.headers.get('Origin','')
        if self.path.startswith('/api/extension/') and re.fullmatch(r'chrome-extension://[a-p]{32}',origin):
            self.send_header('Access-Control-Allow-Origin',origin)
            self.send_header('Vary','Origin')
            self.send_header('Access-Control-Allow-Headers','Content-Type, X-Gem-Extension')
            self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
        self.send_header('X-Content-Type-Options', 'nosniff')
        style_policy="'self' 'unsafe-inline'" if self.path=='/spider-demo' else "'self'"
        self.send_header('Content-Security-Policy', f"default-src 'self'; style-src {style_policy}; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def allowed_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def extension_auth(self):
        return bool(EXTENSION_KEY) and secrets.compare_digest(self.headers.get('X-Gem-Extension',''),EXTENSION_KEY)

    def bearer_auth(self):
        """Scripts on this machine: Authorization: Bearer <data/dashboard-token>. Never sent by browsers implicitly."""
        header = self.headers.get('Authorization', '')
        return bool(DASHBOARD_TOKEN) and header.startswith('Bearer ') and secrets.compare_digest(header[7:].strip(), DASHBOARD_TOKEN)

    def session_auth(self):
        expected = derived('session-v1')
        if not expected:
            return False
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
        except CookieError:
            return False
        morsel = cookie.get(SESSION_COOKIE)
        return morsel is not None and secrets.compare_digest(morsel.value, expected)

    def dashboard_auth(self):
        return self.bearer_auth() or self.session_auth()

    def same_origin(self):
        """Reject cross-site browser requests even if a cookie would be attached."""
        origin = self.headers.get('Origin')
        if origin is not None and origin not in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'):
            return False
        return self.headers.get('Sec-Fetch-Site', 'same-origin') in ('same-origin', 'none')

    def write_auth(self):
        if self.bearer_auth():
            return True
        csrf = derived('csrf-v1')
        return (self.session_auth() and self.same_origin() and bool(csrf)
                and secrets.compare_digest(self.headers.get('X-Gem-Token', ''), csrf))

    def login(self, supplied):
        if not DASHBOARD_TOKEN or not isinstance(supplied, str) or not secrets.compare_digest(supplied, DASHBOARD_TOKEN):
            return self.send(403, LOGIN_PAGE.replace(b'<!--error-->', b'<p class="notice">That sign-in token was not accepted.</p>'), 'text/html; charset=utf-8')
        cookie = f'{SESSION_COOKIE}={derived("session-v1")}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000'
        return self.send(303, b'', 'text/plain; charset=utf-8', [('Set-Cookie', cookie), ('Location', '/')])

    def do_OPTIONS(self):
        if not self.allowed_host() or not self.path.startswith('/api/extension/') or not re.fullmatch(r'chrome-extension://[a-p]{32}',self.headers.get('Origin','')):
            return self.send(403,{'error':'Origin not allowed'})
        return self.send(200,{'ok':True})

    def do_GET(self):
        if not self.allowed_host():
            return self.send(403, {'error': 'Local requests only'})
        parts = urlsplit(self.path)
        path = parts.path
        if path == '/healthz':
            return self.send(200, {'ok': True})
        if path == '/login':
            supplied = parse_qs(parts.query, keep_blank_values=True).get('token', [None])[0]
            if supplied is not None:
                return self.login(supplied)
            return self.send(200, LOGIN_PAGE, 'text/html; charset=utf-8')
        if path == '/' and not self.dashboard_auth():
            return self.send(200, LOGIN_PAGE, 'text/html; charset=utf-8')
        if path=='/api/extension/status':
            if not self.extension_auth():return self.send(403,{'error':'Pairing required'})
            with connect() as con:
                leads=con.execute("SELECT COUNT(*) FROM projects WHERE source='spider' AND status='shortlisted'").fetchone()[0]
            return self.send(200,{'spider':JEV.status(),'grok':GROK.status(),'leads':leads})
        if path == '/api/state':
            if not self.dashboard_auth():
                return self.send(401, {'error': 'Sign-in required', 'login': '/login'})
            with connect() as con:
                projects = [json.loads(r['payload']) for r in con.execute('SELECT payload FROM projects ORDER BY score DESC,updated DESC')]
                runs = [dict(r) for r in con.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 25')]
                events = [dict(r) for r in con.execute('SELECT * FROM events ORDER BY id DESC LIMIT 30')]
                launches = [json.loads(r['payload']) for r in con.execute('SELECT payload FROM launches ORDER BY created DESC')]
            with STATE_LOCK:
                state = dict(STATE)
            # csrf is a derived per-install value: it authorizes nothing without the HttpOnly session cookie.
            return self.send(200, {'state': state, 'projects': projects, 'runs': runs, 'events': events, 'csrf': derived('csrf-v1'),
                                   'launches': launches, 'automation': AUTO.status() if AUTO else None,
                                   'spider':JEV.status() if JEV else None,'pairing_code':EXTENSION_KEY,
                                   'grok':GROK.status() if GROK else None,
                                   'connections': {'x': bool(os.getenv('X_BEARER_TOKEN')) and os.getenv('ENABLE_PAID_X') == '1',
                                                   'launch': (AUTO.provider_status()['label'] + ' · ' + AUTO.mode()) if AUTO else 'offline',
                                                   'free_feed': os.getenv('FREE_FEED_ENABLED', '1') == '1'}})
        files = {'/capsule.js': ('capsule.js', 'text/javascript; charset=utf-8'), '/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
        files.update({'/spider-demo':('../docs/spider-demo.html','text/html; charset=utf-8'),
                      '/spider-demo.js':('../docs/spider-demo.js','text/javascript; charset=utf-8'),
                      '/spider-ui.js':('../extension/spider-ui.js','text/javascript; charset=utf-8')})
        if path in files:
            name, mime = files[path]
            return self.send(200, (ROOT / 'static' / name).read_bytes(), mime)
        return self.send(404, {'error': 'Not found'})

    def do_POST(self):
        extension_request=self.path=='/api/extension/ingest'
        if not self.allowed_host():
            return self.send(403, {'error': 'Local requests only'})
        if self.path == '/login':
            if not self.same_origin():
                return self.send(403, {'error': 'Cross-site sign-in rejected'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                size = -1
            if not 0 <= size <= 4096:
                return self.send(413, {'error': 'Request too large'})
            form = parse_qs(self.rfile.read(size).decode('utf-8', errors='replace'))
            return self.login(form.get('token', [''])[0].strip())
        if not (self.extension_auth() if extension_request else self.write_auth()):
            return self.send(403, {'error': 'Invalid local session'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 <= size <= MAX_BODY:
                return self.send(413, {'error': 'Request exceeds 2 MB'})
            data = json.loads(self.rfile.read(size) or '{}')
            if extension_request:
                return self.send(202,JEV.ingest(data))
            if self.path == '/api/demo':
                started = begin(demo_posts(), 'demo')
            elif self.path == '/api/import':
                started = begin(validate_posts(data), 'import')
            elif self.path == '/api/x':
                started = start_x()
            elif self.path == '/api/launches':
                return self.send(201, create_launch(data))
            elif self.path == '/api/launches/check':
                if not isinstance(data, dict) or not isinstance(data.get('id'), str):
                    raise ValueError('Launch id is required')
                return self.send(200, check_launch(data['id']))
            elif self.path == '/api/launch-control':
                if not AUTO or not isinstance(data, dict) or type(data.get('enabled')) is not bool:
                    raise ValueError('enabled must be boolean')
                pause_file = DATA / 'launch-paused'
                # Pausing takes effect in memory even if the volume is unwritable; the
                # durable marker is written first so a reported success survives restarts.
                if not data['enabled']:
                    AUTO.enabled = False
                    pause_file.touch()
                else:
                    pause_file.unlink(missing_ok=True)
                    AUTO.enabled = True
                event('Automatic launch queue ' + ('resumed' if AUTO.enabled else 'paused'))
                return self.send(200, {'ok': True})
            elif self.path == '/api/autopilot':
                if not isinstance(data, dict) or type(data.get('enabled')) is not bool:
                    raise ValueError('enabled must be boolean')
                with STATE_LOCK:
                    STATE['autopilot'] = data['enabled']
                event('Inbox autopilot ' + ('enabled' if data['enabled'] else 'paused'))
                return self.send(200, {'ok': True})
            else:
                return self.send(404, {'error': 'Not found'})
            return self.send(202 if started else 409, {'ok': bool(started), 'error': None if started else 'A scan is already running'})
        except (ValueError, TypeError, KeyError) as exc:
            return self.send(400, {'error': str(exc)[:300]})
        except (sqlite3.Error, OSError) as exc:
            # No paths, SQL or secrets in responses; the type name is enough to diagnose.
            return self.send(500, {'error': f'Storage error ({type(exc).__name__}); nothing was reported as saved'})


def main():
    global AUTO,JEV,GROK,EXTENSION_KEY,DASHBOARD_TOKEN,RETENTION_DAYS
    load_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--autopilot', action='store_true', help='Watch data/inbox every five minutes')
    parser.add_argument('--login-url', action='store_true', help='Print the dashboard sign-in link and exit')
    args = parser.parse_args()
    if args.login_url:
        DATA.mkdir(exist_ok=True)
        DASHBOARD_TOKEN = ensure_secret(DATA / 'dashboard-token')
        print(login_url(args.port))
        return
    try:
        RETENTION_DAYS = max(1, int(os.getenv('GEM_SEARCH_RETENTION_DAYS', '30')))
    except ValueError:
        RETENTION_DAYS = 30
    init()
    EXTENSION_KEY = ensure_secret(DATA / 'extension-key')
    DASHBOARD_TOKEN = ensure_secret(DATA / 'dashboard-token')
    JEV=Jev(connect)
    GROK=GrokReview(connect)
    AUTO = Automation(ROOT, DATA, connect, event)
    STATE['autopilot'] = args.autopilot
    bind_host = os.environ.get('GEM_SEARCH_HOST', '127.0.0.1')
    server = ThreadingHTTPServer((bind_host, args.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=autopilot, daemon=True).start()
    threading.Thread(target=launch_loop, daemon=True).start()
    threading.Thread(target=spider_loop, daemon=True).start()
    print(f'Gem Search → http://127.0.0.1:{args.port} · launch mode: {AUTO.mode()}', flush=True)
    # The sign-in link is a credential: show it on an interactive terminal, never in service logs.
    if sys.stdout.isatty():
        print(f'Sign in: {login_url(args.port)}', flush=True)
    else:
        print('Sign-in link: run `python app.py --login-url` (Docker: docker exec gem-search python app.py --login-url)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        STOP.set()
        server.server_close()


if __name__ == '__main__':
    main()
