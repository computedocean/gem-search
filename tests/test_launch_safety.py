"""Fail-closed launch mode, feed/queue separation, execution-boundary checks and persistence."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from automation import Automation, discover_free, launch_mode
from jev import Jev


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous = app.DATA, app.DB
        app.DATA = Path(self.temp.name)
        app.DB = app.DATA / 'test.sqlite'
        app.init()
        self.env = patch.dict(os.environ, {'LAUNCH_MODE': 'dry_run', 'LAUNCH_PROVIDER': 'pumpportal'})
        self.env.start()
        os.environ.pop('GEM_SEARCH_DRY_RUN_LOCK', None)
        self.engine = Automation(app.ROOT, app.DATA, app.connect, app.event)

    def tearDown(self):
        self.env.stop()
        app.DATA, app.DB = self.previous
        self.temp.cleanup()

    def project(self, i, **kw):
        return dict({'source': 'import', 'status': 'shortlisted', 'url': f'https://example.com/{i}',
                     'name': f'Project {i}', 'observed_at': time.time()}, **kw)

    def jobs(self):
        with app.connect() as con:
            return [dict(r) for r in con.execute('SELECT * FROM launch_jobs ORDER BY created')]


class LaunchModeTests(Base):
    def test_only_exact_live_enables_live(self):
        for value in ['', 'LIVE', 'Live', ' live', 'live ', 'live\n', 'dry_run', 'true', '1', 'live,dry_run', 'lіve']:
            with self.subTest(value=value), patch.dict(os.environ, {'LAUNCH_MODE': value}):
                self.assertEqual(launch_mode(), 'dry_run')
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LAUNCH_MODE', None)
            self.assertEqual(launch_mode(), 'dry_run')
        with patch.dict(os.environ, {'LAUNCH_MODE': 'live'}):
            self.assertEqual(launch_mode(), 'live')

    def test_deployment_lock_overrides_live(self):
        with patch.dict(os.environ, {'LAUNCH_MODE': 'live', 'GEM_SEARCH_DRY_RUN_LOCK': '1'}):
            self.assertEqual(launch_mode(), 'dry_run')
            self.engine.enqueue(self.project(1))
            with patch('automation.subprocess.run') as execute, patch('automation.run_bridge') as bridge:
                self.engine.tick()
                execute.assert_not_called()
                bridge.assert_not_called()
        self.assertEqual([j['mode'] for j in self.jobs()], ['dry_run'])

    def test_hostile_project_fields_cannot_choose_mode_or_provider(self):
        hostile = self.project(1, mode='live', provider={'kind': 'bridge', 'endpoint': 'https://evil.example'},
                               amount=5, LAUNCH_MODE='live', status='shortlisted')
        self.assertTrue(self.engine.enqueue(hostile))
        job = self.jobs()[0]
        draft = json.loads(job['payload'])
        self.assertEqual((job['mode'], draft['mode'], draft['amount'], draft['provider']['kind']), ('dry_run', 'dry_run', 0, 'pumpportal'))

    def test_dry_run_job_never_becomes_live_after_env_change(self):
        self.engine.enqueue(self.project(1))
        with patch.dict(os.environ, {'LAUNCH_MODE': 'live'}), patch.object(self.engine, 'missing', return_value=[]), \
                patch('automation.subprocess.run') as execute:
            self.engine.tick()
            execute.assert_not_called()
        self.assertEqual([j['status'] for j in self.jobs()], ['queued'])
        self.engine.tick()
        self.assertEqual([j['status'] for j in self.jobs()], ['dry_run_complete'])

    def test_configuration_rechecked_at_execution_boundary(self):
        with patch.dict(os.environ, {'LAUNCH_MODE': 'live'}):
            self.engine.enqueue(self.project(1))
            # Credentials vanish between reservation and execution.
            with patch.object(self.engine, 'missing', side_effect=[[], ['PINATA_JWT']]), \
                    patch('automation.subprocess.run') as execute:
                self.engine.tick()
                execute.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'], 'pending')

    def test_mode_change_mid_tick_stops_provider(self):
        with patch.dict(os.environ, {'LAUNCH_MODE': 'live'}):
            self.engine.enqueue(self.project(1))
        with patch('automation.launch_mode', side_effect=['live', 'dry_run']), \
                patch.object(self.engine, 'missing', return_value=[]), patch('automation.subprocess.run') as execute:
            self.engine.tick()
            execute.assert_not_called()

    def test_tampered_job_is_blocked_before_provider(self):
        with patch.dict(os.environ, {'LAUNCH_MODE': 'live'}):
            self.engine.enqueue(self.project(1))
            job = self.jobs()[0]
            for tamper in [{'amount': 1}, {'mode': 'dry_run'}, {'id': '../../etc'}, {'symbol': 'bad symbol'}]:
                draft = dict(json.loads(job['payload']), **tamper)
                with app.connect() as con:
                    con.execute("UPDATE launch_jobs SET status='queued',reserved=NULL,payload=? WHERE id=?", (json.dumps(draft), job['id']))
                with self.subTest(tamper=tamper), patch.object(self.engine, 'missing', return_value=[]), \
                        patch('automation.subprocess.run') as execute, patch('automation.run_bridge') as bridge:
                    self.engine.tick()
                    execute.assert_not_called()
                    bridge.assert_not_called()
                    self.assertEqual(self.jobs()[0]['status'], 'blocked')

    def test_concurrent_topic_enqueue_creates_one_job(self):
        barrier = threading.Barrier(8)
        results = []

        def worker(i):
            barrier.wait()
            results.append(self.engine.enqueue(self.project(i, topic='agents', posts=[{'id': f'evidence-{i}'}])))
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.jobs()), 1)

    def test_restart_does_not_duplicate_or_rerun_jobs(self):
        for i in range(3):
            self.engine.enqueue(self.project(i))
        for _ in range(3):
            self.engine.tick()
        before = self.jobs()
        restarted = Automation(app.ROOT, app.DATA, app.connect, app.event)
        for i in range(3):
            self.assertFalse(restarted.enqueue(self.project(i)))
        restarted.tick()
        self.assertEqual(self.jobs(), before)


class FeedTests(Base):
    def test_hostile_feed_is_research_only_in_dry_run(self):
        now = int(time.time())
        items = {1: {'type': 'story', 'title': 'AI agents ship', 'url': 'https://a.example/x', 'by': 'a', 'score': 90, 'descendants': 30, 'time': now},
                 2: {'type': 'story', 'title': 'Agentic LLM release', 'url': 'https://b.example', 'by': 'b', 'score': 50, 'descendants': 10, 'time': now},
                 3: {'type': 'story', 'title': 'LLM agents paper', 'url': 'https://c.example', 'by': 'c', 'score': 40, 'descendants': 5, 'time': now},
                 4: {'type': 'story', 'title': 'AI agents', 'url': 'javascript:alert(1)', 'by': 'd', 'score': 99, 'time': now},
                 5: {'type': 'story', 'title': 'Stale agents', 'url': 'https://old.example', 'by': 'e', 'score': 99, 'time': now - 3 * 86400},
                 6: {'type': 'comment', 'title': 'agents', 'url': 'https://x.example', 'score': 99, 'time': now},
                 7: None}

        def fetch(url):
            if url.endswith('topstories.json'):
                return url, json.dumps([1, 1, 2, 3, 4, 5, 6, 7, 8, '9; DROP TABLE', {'id': 1}, 10])
            identity = int(url.rsplit('/', 1)[1].split('.')[0])
            if identity == 8:
                return url, '{malformed'
            if identity == 10:
                raise TimeoutError('slow')
            return url, json.dumps(items.get(identity))
        projects = discover_free(self.engine, fetch)
        agents = [p for p in projects if p['topic'] == 'agents']
        self.assertEqual(len(agents), 1)
        self.assertEqual({e['url'] for e in agents[0]['evidence']['pages']}, {'https://a.example/x', 'https://b.example', 'https://c.example'})
        with patch('automation.subprocess.run') as execute, patch('automation.run_bridge') as bridge:
            for p in projects:
                self.engine.enqueue(p)
            self.engine.tick()
            execute.assert_not_called()
            bridge.assert_not_called()
        self.assertTrue(all(j['mode'] == 'dry_run' for j in self.jobs()))
        # Second poll inside 30 minutes is a no-op, not a second fetch storm.
        self.assertEqual(discover_free(self.engine, lambda url: self.fail('fetched during cooldown')), [])


class PersistenceTests(Base):
    def write_inbox(self, name, value):
        path = app.DATA / 'inbox' / name
        path.write_text(json.dumps(value) if not isinstance(value, str) else value)
        return path

    def post(self, i):
        return {'id': f'p{i}', 'author': f'a{i}', 'text': f'note {i}', 'project_url': 'https://example.com',
                'created_at': '2026-01-01T00:00:00Z'}

    def test_secret_files_atomic_private_and_recovered(self):
        path = app.DATA / 'dashboard-token'
        first = app.ensure_secret(path)
        self.assertEqual(app.ensure_secret(path), first)
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        path.write_text('')  # interrupted legacy write
        second = app.ensure_secret(path)
        self.assertNotEqual(second, first)
        self.assertRegex(second, r'^[A-Za-z0-9_-]{40,100}$')
        self.assertEqual([p.name for p in app.DATA.iterdir() if p.name.endswith('.tmp')], [])

    def test_inbox_success_invalid_and_retry(self):
        good = self.write_inbox('a-good.json', [self.post(1)])
        bad = self.write_inbox('b-bad.json', '{not json')
        with patch.object(app, 'cached_crawl', return_value={'pages': [], 'errors': [], 'synthetic': False}):
            app.process_inbox()
        self.assertFalse(good.exists())
        self.assertFalse(bad.exists())
        self.assertEqual(len(list((app.DATA / 'processed').iterdir())), 1)
        failed = list((app.DATA / 'failed').iterdir())
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].read_text(), '{not json')  # preserved, not deleted

    def test_storage_failure_keeps_file_for_retry(self):
        path = self.write_inbox('retry.json', [self.post(2)])
        with patch.object(app, 'cached_crawl', side_effect=OSError(28, 'No space left on device')):
            app.process_inbox()
        self.assertTrue(path.exists())
        self.assertEqual(list((app.DATA / 'processed').iterdir()), [])
        with patch.object(app, 'cached_crawl', return_value={'pages': [], 'errors': [], 'synthetic': False}):
            app.process_inbox()
        self.assertFalse(path.exists())

    def test_busy_scanner_leaves_inbox_untouched(self):
        path = self.write_inbox('busy.json', [self.post(3)])
        self.assertTrue(app.SCAN_LOCK.acquire(blocking=False))
        try:
            app.process_inbox()
        finally:
            app.SCAN_LOCK.release()
        self.assertTrue(path.exists())

    def test_repeated_processing_is_idempotent(self):
        with patch.object(app, 'cached_crawl', return_value={'pages': [], 'errors': [], 'synthetic': False}):
            for name in ('one.json', 'two.json'):
                self.write_inbox(name, [self.post(4), self.post(5)])
                app.process_inbox()
        with app.connect() as con:
            self.assertEqual([r[0] for r in con.execute('SELECT posts FROM runs ORDER BY id')], [2, 0])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM seen').fetchone()[0], 2)

    def test_retention_only_touches_old_archives(self):
        old = time.time() - 40 * 86400
        archive = app.DATA / 'processed' / 'old.json'
        failed = app.DATA / 'failed' / 'old.json'
        recent = app.DATA / 'processed' / 'recent.json'
        inbox = app.DATA / 'inbox' / 'old.json'
        job = app.DATA / 'launch-jobs' / ('a' * 24) / 'state.json'
        job.parent.mkdir(parents=True)
        for path in (archive, failed, recent, inbox, job):
            path.write_text('{}')
        for path in (archive, failed, inbox, job):
            os.utime(path, (old, old))
        self.assertEqual(app.prune_files(30), 2)
        self.assertEqual([p.exists() for p in (archive, failed, recent, inbox, job)], [False, False, True, True, True])

    def test_scan_db_failure_reports_failure_not_success(self):
        real = app.connect
        calls = {'n': 0}

        def flaky():
            calls['n'] += 1
            if calls['n'] > 2:  # seen + observations reads succeed; the final write fails
                raise app.sqlite3.OperationalError('database or disk is full')
            return real()
        with patch.object(app, 'connect', side_effect=flaky), \
                patch.object(app, 'cached_crawl', return_value={'pages': [], 'errors': [], 'synthetic': False}), \
                patch.object(app, 'event'):
            self.assertFalse(app.begin([self.post(6)], 'import', background=False))
        self.assertFalse(app.SCAN_LOCK.locked())
        with app.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM seen').fetchone()[0], 0)

    def test_stale_spider_posts_cannot_block_capture(self):
        jev = Jev(app.connect)
        with app.connect() as con:
            con.executemany('INSERT INTO spider_posts(id,created,processed,payload) VALUES (?,?,0,?)',
                            [(f'old{i}', time.time() - 2 * 86400, '{}') for i in range(500)])
        post = {'url': 'https://x.com/a/status/1', 'text': 'AI agents', 'created_at': '2026-10-10T00:00:00Z', 'links': []}
        with patch('jev.normalize_capture', return_value=[{'id': 'fresh', 'timestamp': time.time()}]):
            self.assertEqual(jev.ingest({'posts': [post]})['accepted'], 1)
        with app.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM spider_posts').fetchone()[0], 501)  # retired, not deleted


if __name__ == '__main__':
    unittest.main()
