"""Run against an already running DRY-RUN localhost server only.

Sign-in token: GEM_SEARCH_TOKEN, else data/dashboard-token. It is never printed.
"""
import json
import os
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError

BASE = os.getenv('GEM_SEARCH_URL', 'http://127.0.0.1:8787')
TOKEN = os.getenv('GEM_SEARCH_TOKEN') or (Path(__file__).resolve().parents[1] / 'data' / 'dashboard-token').read_text().strip()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def request(path, body=None, headers=None):
    req = Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                  headers=dict({'Content-Type': 'application/json'}, **(headers or {})))
    try:
        with OPENER.open(req, timeout=10) as response:
            return response.status, response.read(), response.headers
    except HTTPError as error:
        return error.code, error.read(), error.headers


bearer = {'Authorization': 'Bearer ' + TOKEN}
assert request('/healthz')[0] == 200
status, page, _ = request('/')
assert status == 200 and b'Sign in' in page and TOKEN.encode() not in page
for path in ['/style.css', '/app.js']:
    assert request(path)[0] == 200

# Unauthenticated state discloses nothing.
status, body, _ = request('/api/state')
assert status == 401 and TOKEN.encode() not in body and b'pairing' not in body
# Every privileged route rejects missing, malformed and wrong credentials.
for route in ['/api/launch-control', '/api/autopilot', '/api/import', '/api/x', '/api/launches', '/api/demo']:
    for headers in [{}, {'X-Gem-Token': 'wrong'}, {'Authorization': 'Bearer wrong'}, {'Authorization': TOKEN}]:
        assert request(route, {'enabled': False}, headers)[0] == 403, route

# Browser flow: login sets an HttpOnly cookie; state then returns a CSRF value, not the token.
status, _, headers = request('/login?token=' + TOKEN)
cookie = headers['Set-Cookie']
assert status == 303 and 'HttpOnly' in cookie and 'SameSite=Strict' in cookie and TOKEN not in cookie
session = {'Cookie': cookie.split(';')[0]}
status, body, _ = request('/api/state', headers=session)
state = json.loads(body)
assert status == 200 and TOKEN.encode() not in body and 'token' not in state
assert state['automation']['mode'] == 'dry_run'
assert state['automation']['max_sol'] == .025 and state['automation']['daily_max'] == 5
assert state['connections']['x'] is False
assert request('/api/launch-control', {'enabled': False}, session)[0] == 403, 'cookie without CSRF header'
assert request('/api/launch-control', {'enabled': False}, dict(session, **{'X-Gem-Token': state['csrf'], 'Origin': 'https://evil.example'}))[0] == 403
enabled = state['automation']['enabled']
browser = dict(session, **{'X-Gem-Token': state['csrf'], 'Origin': BASE})
try:
    assert request('/api/launch-control', {'enabled': False}, browser)[0] == 200
    assert json.loads(request('/api/state', headers=bearer)[1])['automation']['enabled'] is False
finally:
    request('/api/launch-control', {'enabled': enabled}, bearer)
print('HTTP OK: health, sign-in, no secret disclosure, privileged routes rejected, CSRF, dry-run, pause/resume.')
