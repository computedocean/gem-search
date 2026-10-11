import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from jev import Jev,normalize_capture
from grok import GrokReview,validate_vote


class SpiderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old=app.DATA,app.DB
        app.DATA=Path(self.temp.name);app.DB=app.DATA/'test.sqlite';app.init()
        self.jev=Jev(app.connect);self.grok=GrokReview(app.connect)

    def tearDown(self):
        app.DATA,app.DB=self.old;self.temp.cleanup()

    def post(self,i):
        return {'url':f'https://x.com/researcher{i}/status/{100+i}','created_at':datetime.now(timezone.utc).isoformat(),
                'text':f'AI agent release {i}: reproducible experiment {i*13}', 'links':[f'https://project{i}.example/docs']}

    def test_capture_dedup_and_durable_ack(self):
        p=self.post(1)
        self.assertEqual(self.jev.ingest({'posts':[p,p]})['accepted'],1)
        self.assertEqual(self.jev.status()['pending'],1)
        self.jev.ack(self.jev.pending())
        self.assertEqual(Jev(app.connect).status()['pending'],0)

    def test_invalid_capture_and_future_dates(self):
        for url in ['https://x.com/messages','https://other.com/a/status/123','https://x.com@evil.test/a/status/123','http://x.com/a/status/123']:
            with self.assertRaises(ValueError):normalize_capture({'posts':[dict(self.post(1),url=url)]})
        with self.assertRaises(ValueError):normalize_capture({'posts':[dict(self.post(1),created_at='2099-01-01T00:00:00Z')]})

    def test_narrative_from_distinct_authors_and_reject_risk(self):
        self.jev.ingest({'posts':[self.post(i) for i in range(5)]})
        p=self.jev.narratives()[0]
        self.assertEqual(p['status'],'shortlisted');self.assertEqual(p['source'],'spider')
        self.jev.ingest({'posts':[dict(self.post(9),text='AI agent guaranteed profit')]})
        self.assertEqual(self.jev.narratives()[0]['status'],'rejected')

    def test_grok_never_calls_network_unconfigured(self):
        with patch.dict(os.environ,{'GROK_ENABLED':'0','XAI_API_KEY':''}),patch('grok.urlopen') as request:
            self.assertEqual(self.grok.review({'name':'test'})['status'],'disabled');request.assert_not_called()

    def test_unavailable_grok_cannot_demote_a_valid_local_verdict(self):
        project={'source':'spider','status':'shortlisted','signals':{'authors':5},'votes':[]}
        class FakeGrok:
            def __init__(self,configured,result):self.configured=configured;self.result=result
            def status(self):return {'enabled':True,'configured':self.configured}
            def review(self,_project):return self.result
        for configured,result in [(False,{'status':'disabled','votes':[]}),(True,{'status':'budget_exhausted','votes':[]}),(True,{'status':'partial','votes':[{'vote':'hold','available':False}]})]:
            with patch.object(app,'GROK',FakeGrok(configured,result)):
                current=dict(project)
                self.assertEqual(app.apply_grok(current)['status'],'shortlisted')

    def test_grok_parallel_review_cache_and_persistent_budget(self):
        value={'vote':'pass','reason':'Evidence observed','evidence_ids':['post-0'],'unknowns':['Team identity']}
        body=json.dumps({'choices':[{'message':{'content':json.dumps(value)}}]}).encode()
        project={'name':'test','posts':[{'text':'Some evidence'}]}
        with patch.dict(os.environ,{'GROK_ENABLED':'1','XAI_API_KEY':'test-key','GROK_DAILY_CALLS':'4'}),patch('grok.urlopen',side_effect=lambda *a,**k:io.BytesIO(body)) as request:
            self.assertEqual(self.grok.review(project)['status'],'complete');self.assertEqual(request.call_count,4)
            self.assertTrue(self.grok.review(project)['cached']);self.assertEqual(request.call_count,4)
            other=GrokReview(app.connect)
            self.assertEqual(other.review(dict(project,name='different'))['status'],'budget_exhausted')

    def test_grok_failure_is_hold_and_still_charged_to_allowance(self):
        with patch.dict(os.environ,{'GROK_ENABLED':'1','XAI_API_KEY':'test-key','GROK_DAILY_CALLS':'4'}),patch('grok.urlopen',side_effect=TimeoutError):
            result=self.grok.review({'name':'test','posts':[{'text':'Untrusted input'}]})
            self.assertEqual(result['status'],'partial');self.assertTrue(all(v['vote']=='hold' for v in result['votes']));self.assertEqual(self.grok.status()['calls_used'],4)

    def test_grok_rejects_invented_evidence_and_missing_citations(self):
        for ids in [[],['not-provided']]:
            with self.assertRaises(ValueError):validate_vote({'vote':'pass','reason':'Trust me','evidence_ids':ids,'unknowns':[]},{'post-0'})


if __name__=='__main__':unittest.main()
