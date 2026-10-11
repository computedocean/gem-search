"""Isolated loopback integration check; no real account data or external requests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from jev import Jev
from grok import GrokReview

with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'GROK_ENABLED':'0'}):
    app.DATA=Path(temp);app.DB=app.DATA/'test.sqlite';app.init()
    app.JEV=Jev(app.connect);app.GROK=GrokReview(app.connect);app.EXTENSION_KEY='a'*43;app.DASHBOARD_TOKEN='d'*43
    server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    def req(path,body=None,key='',bearer=''):
        headers={'X-Gem-Extension':key,'Content-Type':'application/json'}
        if bearer:headers['Authorization']='Bearer '+bearer
        request=Request(base+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
        try:
            with urlopen(request,timeout=5) as response:return response.status,response.read()
        except HTTPError as e:return e.code,e.read()
    try:
        assert req('/api/extension/status')[0]==403
        assert req('/api/extension/status',key=app.EXTENSION_KEY)[0]==200
        assert req('/api/launch-control',{'enabled':True},app.EXTENSION_KEY)[0]==403
        assert req('/spider-demo')[0]==200
        assert req('/capsule.js')[0]==200
        posts=[{'url':f'https://x.com/test{i}/status/{100+i}','text':f'AI agents experiment {i} version {i*19}',
                'created_at':datetime.now(timezone.utc).isoformat(),'links':['https://example.com']} for i in range(5)]
        status,body=req('/api/extension/ingest',{'posts':posts},app.EXTENSION_KEY)
        assert status==202 and json.loads(body)['accepted']==5
        assert json.loads(req('/api/extension/ingest',{'posts':posts},app.EXTENSION_KEY)[1])['accepted']==0
        class OneIteration:
            count=0
            def wait(self,_):self.count+=1;return self.count>1
        with patch.object(app,'STOP',OneIteration()),patch.object(app,'cached_crawl',return_value={'pages':[],'errors':[],'synthetic':False}):
            app.spider_loop()
        assert app.JEV.status()['pending']==0
        assert not app.SCAN_LOCK.locked()
        assert app.STATE['running'] is False
        assert req('/api/state',key=app.EXTENSION_KEY)[0]==401
        state=json.loads(req('/api/state',bearer=app.DASHBOARD_TOKEN)[1]);assert any(p['id']=='spider-agents' for p in state['projects'])
        assert all(p['source']=='spider' for p in state['projects'])
        print('Extension HTTP + pipeline OK: pairing, scope isolation, capture, dedup, crawler handoff, narrative, durable ack, demo assets.')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
