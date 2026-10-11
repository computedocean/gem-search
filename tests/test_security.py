"""Dashboard authorization, secret disclosure and error-leak regressions. Loopback only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from automation import Automation
from grok import GrokReview
from jev import Jev

TOKEN = 'D' * 43
PAIRING = 'P' * 43
PRIVILEGED = ['/api/demo', '/api/import', '/api/x', '/api/launches', '/api/launches/check',
              '/api/launch-control', '/api/autopilot']


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DashboardAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.saved = {k: getattr(app, k) for k in ('DATA', 'DB', 'AUTO', 'JEV', 'GROK', 'EXTENSION_KEY', 'DASHBOARD_TOKEN')}
        cls.env = patch.dict(os.environ, {'LAUNCH_MODE': 'dry_run', 'GROK_ENABLED': '0', 'ENABLE_PAID_X': '0',
                                          'XAI_API_KEY': 'xai-secret-value', 'LAUNCH_BRIDGE_TOKEN': 'bridge-secret-value'})
        cls.env.start()
        app.DATA = Path(cls.temp.name)
        app.DB = app.DATA / 'test.sqlite'
        app.init()
        app.JEV, app.GROK = Jev(app.connect), GrokReview(app.connect)
        app.AUTO = Automation(app.ROOT, app.DATA, app.connect, app.event)
        app.EXTENSION_KEY, app.DASHBOARD_TOKEN = PAIRING, TOKEN
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.opener = build_opener(NoRedirect())

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.env.stop()
        for key, value in cls.saved.items():
            setattr(app, key, value)
        cls.temp.cleanup()

    def setUp(self):
        app.AUTO.enabled = True
        (app.DATA / 'launch-paused').unlink(missing_ok=True)

    def req(self, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        request = Request(self.base + path, data=data, headers=dict({'Content-Type': 'application/json'}, **(headers or {})))
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except HTTPError as error:
            return error.code, error.read(), error.headers

    def session(self):
        status, _, headers = self.req('/login?token=' + TOKEN)
        self.assertEqual(status, 303)
        return {'Cookie': headers['Set-Cookie'].split(';')[0]}

    def browser(self):
        session = self.session()
        state = json.loads(self.req('/api/state', headers=session)[1])
        return dict(session, **{'X-Gem-Token': state['csrf'], 'Origin': self.base})

    def assertNoSecrets(self, body):
        for secret in (TOKEN, PAIRING, 'xai-secret-value', 'bridge-secret-value'):
            self.assertNotIn(secret.encode(), body)

    def test_unauthenticated_responses_disclose_no_secret(self):
        status, body, _ = self.req('/api/state')
        self.assertEqual(status, 401)
        self.assertNoSecrets(body)
        for path in ['/', '/login', '/healthz', '/app.js', '/style.css', '/capsule.js', '/spider-demo',
                     '/api/extension/status', '/missing']:
            self.assertNoSecrets(self.req(path)[1])
        self.assertEqual(self.req('/healthz')[1], b'{"ok": true}')

    def test_values_from_unauthenticated_responses_cannot_authorize(self):
        harvested = set()
        for path in ['/', '/login', '/healthz', '/api/state', '/app.js']:
            _, body, headers = self.req(path)
            harvested.update(word for word in body.decode('utf-8', 'replace').replace('"', ' ').split() if len(word) >= 20 and word.isascii())
            if headers.get('Set-Cookie'):
                harvested.add(headers['Set-Cookie'])
        for value in harvested:
            for headers in [{'X-Gem-Token': value}, {'Authorization': 'Bearer ' + value}, {'Cookie': 'gem_session=' + value}]:
                self.assertEqual(self.req('/api/launch-control', {'enabled': False}, headers)[0], 403)
        self.assertTrue(app.AUTO.enabled)

    def test_every_privileged_route_rejects_bad_credentials(self):
        session = self.session()
        csrf = json.loads(self.req('/api/state', headers=session)[1])['csrf']
        rejected = [{}, {'X-Gem-Token': ''}, {'X-Gem-Token': 'wrong'}, {'X-Gem-Token': TOKEN},
                    {'Authorization': 'Bearer'}, {'Authorization': 'Bearer wrong'}, {'Authorization': TOKEN},
                    {'Authorization': 'Basic ' + TOKEN}, {'Authorization': 'Bearer ' + TOKEN[:-1]},
                    {'X-Gem-Extension': PAIRING}, {'X-Gem-Token': csrf},
                    session, dict(session, **{'X-Gem-Token': 'wrong'}),
                    dict(session, **{'X-Gem-Token': csrf, 'Origin': 'https://evil.example'}),
                    dict(session, **{'X-Gem-Token': csrf, 'Origin': 'null'}),
                    dict(session, **{'X-Gem-Token': csrf, 'Sec-Fetch-Site': 'cross-site'}),
                    {'Cookie': 'gem_session=forged', 'X-Gem-Token': csrf}, {'Cookie': '\x7f;;=bad'}]
        for route in PRIVILEGED:
            for headers in rejected:
                with self.subTest(route=route, headers=sorted(headers)):
                    self.assertEqual(self.req(route, {'enabled': False}, headers)[0], 403)
        self.assertTrue(app.AUTO.enabled)
        self.assertFalse((app.DATA / 'launch-paused').exists())

    def test_wrong_host_rejected_even_with_credentials(self):
        for path in ['/api/state', '/healthz']:
            self.assertEqual(self.req(path, headers={'Host': 'evil.example', 'Authorization': 'Bearer ' + TOKEN})[0], 403)
        self.assertEqual(self.req('/api/launch-control', {'enabled': False}, {'Host': 'rebind.example:80', 'Authorization': 'Bearer ' + TOKEN})[0], 403)

    def test_each_privileged_route_authorized_explicitly(self):
        browser = self.browser()
        bearer = {'Authorization': 'Bearer ' + TOKEN}
        self.assertEqual(self.req('/api/launch-control', {'enabled': False}, browser)[0], 200)
        self.assertFalse(app.AUTO.enabled)
        self.assertEqual(self.req('/api/launch-control', {'enabled': True}, bearer)[0], 200)
        self.assertTrue(app.AUTO.enabled)
        self.assertEqual(self.req('/api/autopilot', {'enabled': False}, browser)[0], 200)
        self.assertEqual(self.req('/api/x', {}, browser)[0], 400)  # authorized, but paid X stays disabled
        status, body, _ = self.req('/api/launches', {'name': 'Draft', 'symbol': 'drf', 'description': 'Local only'}, browser)
        self.assertEqual(status, 201)
        self.assertEqual(self.req('/api/launches/check', {'id': json.loads(body)['id']}, bearer)[0], 200)
        with patch.object(app, 'begin', return_value=True) as begin:
            self.assertEqual(self.req('/api/demo', {}, browser)[0], 202)
            post = {'id': '1', 'author': 'a', 'text': 't', 'project_url': 'https://example.com', 'created_at': '2026-01-01T00:00:00Z'}
            self.assertEqual(self.req('/api/import', [post], bearer)[0], 202)
            self.assertEqual(begin.call_count, 2)

    def test_dashboard_state_and_pairing_only_after_sign_in(self):
        status, page, _ = self.req('/')
        self.assertEqual(status, 200)
        self.assertIn(b'Sign in', page)
        session = self.session()
        self.assertIn(b'automation-panel', self.req('/', headers=session)[1])
        status, body, _ = self.req('/api/state', headers=session)
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertNotIn('token', state)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(state['pairing_code'], PAIRING)
        self.assertNotIn(b'xai-secret-value', body)
        self.assertNotIn(b'bridge-secret-value', body)

    def test_login_flow(self):
        status, body, headers = self.req('/login?token=wrong')
        self.assertEqual(status, 403)
        self.assertIsNone(headers.get('Set-Cookie'))
        self.assertNoSecrets(body)
        status, _, headers = self.req('/login?token=' + TOKEN)
        cookie = headers['Set-Cookie']
        self.assertEqual((status, headers['Location']), (303, '/'))
        for flag in ('HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(flag, cookie)
        self.assertNotIn(TOKEN, cookie)
        form = {'Content-Type': 'application/x-www-form-urlencoded'}
        self.assertEqual(self.req('/login', raw=('token=' + TOKEN).encode(), headers=dict(form, Origin=self.base))[0], 303)
        self.assertEqual(self.req('/login', raw=('token=' + TOKEN).encode(), headers=dict(form, Origin='https://evil.example'))[0], 403)
        self.assertEqual(self.req('/login', raw=b'token=nope', headers=form)[0], 403)

    def test_no_token_configured_fails_closed(self):
        with patch.object(app, 'DASHBOARD_TOKEN', ''):
            self.assertEqual(self.req('/api/state', headers={'Authorization': 'Bearer '})[0], 401)
            self.assertEqual(self.req('/api/launch-control', {'enabled': False}, {'Authorization': 'Bearer '})[0], 403)
            self.assertEqual(self.req('/login?token=')[0], 403)

    def test_storage_errors_do_not_leak_or_report_success(self):
        bearer = {'Authorization': 'Bearer ' + TOKEN}
        failure = sqlite3.OperationalError('disk I/O error at /app/data/gem-search.sqlite ' + TOKEN)
        with patch.object(app, 'connect', side_effect=failure):
            status, body, _ = self.req('/api/launches', {'name': 'X', 'symbol': 'X', 'description': 'd'}, bearer)
        self.assertEqual(status, 500)
        self.assertNoSecrets(body)
        self.assertNotIn(b'/app/data', body)

    def test_pause_write_failure_is_reported_and_stays_safe(self):
        bearer = {'Authorization': 'Bearer ' + TOKEN}
        with patch.object(Path, 'touch', side_effect=OSError(28, 'No space left on device')):
            self.assertEqual(self.req('/api/launch-control', {'enabled': False}, bearer)[0], 500)
        self.assertFalse(app.AUTO.enabled)
        (app.DATA / 'launch-paused').touch()
        with patch.object(Path, 'unlink', side_effect=OSError(30, 'Read-only file system')):
            self.assertEqual(self.req('/api/launch-control', {'enabled': True}, bearer)[0], 500)
        self.assertFalse(app.AUTO.enabled)

    def test_routine_output_and_events_contain_no_secret(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.req('/login?token=wrong')
            self.req('/api/launch-control', {'enabled': False}, {'X-Gem-Token': 'wrong'})
            self.req('/api/launch-control', {'enabled': True}, {'Authorization': 'Bearer ' + TOKEN})
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        with app.connect() as con:
            events = ' '.join(r['message'] for r in con.execute('SELECT message FROM events'))
        for secret in (TOKEN, PAIRING, 'xai-secret-value', 'bridge-secret-value'):
            self.assertNotIn(secret, events)

    def test_extension_key_cannot_use_dashboard_routes_and_vice_versa(self):
        self.assertEqual(self.req('/api/extension/status', headers={'X-Gem-Extension': PAIRING})[0], 200)
        self.assertEqual(self.req('/api/extension/status', headers={'Authorization': 'Bearer ' + TOKEN})[0], 403)
        self.assertEqual(self.req('/api/extension/ingest', {'posts': []}, {'Authorization': 'Bearer ' + TOKEN})[0], 403)
        self.assertEqual(self.req('/api/state', headers={'X-Gem-Extension': PAIRING})[0], 401)


if __name__ == '__main__':
    unittest.main()
