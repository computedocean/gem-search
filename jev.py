"""JEV is Gem Search's local signal detector, not a third-party service."""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from automation import TOPICS


def normalize_capture(data):
    posts = data.get('posts') if isinstance(data, dict) else None
    if not isinstance(posts,list) or not 1<=len(posts)<=20:
        raise ValueError('Expected 1–20 captured posts')
    result=[]
    for p in posts:
        if not isinstance(p,dict): raise ValueError('Invalid post')
        url=p.get('url','')
        parsed=urlsplit(url)
        match=re.fullmatch(r'/([A-Za-z0-9_]{1,30})/status/(\d{1,30})',parsed.path)
        if parsed.scheme!='https' or parsed.hostname not in ('x.com','twitter.com','www.x.com','www.twitter.com') or not match or parsed.username or parsed.port:
            raise ValueError('Expected an X post permalink')
        text=p.get('text')
        if not isinstance(text,str) or not 1<=len(text)<=10000:raise ValueError('Invalid post text')
        stamp=datetime.fromisoformat(str(p.get('created_at','')).replace('Z','+00:00'))
        if stamp.tzinfo is None or stamp.timestamp()>time.time()+300:raise ValueError('Invalid post time')
        links=p.get('links',[])
        if not isinstance(links,list) or len(links)>8:raise ValueError('Invalid links')
        clean=[]
        for link in links:
            if not isinstance(link,str) or len(link)>2048:continue
            u=urlsplit(link)
            if u.scheme in ('http','https') and u.hostname and not u.username and not u.password and u.port in (None,80,443):
                if u.hostname not in ('x.com','twitter.com','t.co','www.x.com','www.twitter.com'):
                    clean.append(link)
        result.append({'id':match[2],'author':match[1],'text':text,'url':f'https://x.com/{match[1]}/status/{match[2]}',
                       'created_at':stamp.isoformat(),'timestamp':stamp.timestamp(),'links':clean,'captured_at':time.time()})
    return result


class Jev:
    def __init__(self, connect):
        self.connect=connect
        with connect() as con:
            con.executescript('''CREATE TABLE IF NOT EXISTS spider_posts (id TEXT PRIMARY KEY, created REAL, processed INTEGER DEFAULT 0, payload TEXT);
            CREATE TABLE IF NOT EXISTS spider_meta (key TEXT PRIMARY KEY, value TEXT);''')

    def ingest(self,data):
        posts=normalize_capture(data)
        added=0
        with self.connect() as con:
            # Posts older than the 24h narrative window can never contribute; retire them so a
            # stuck batch cannot block capture forever. Kept as processed rows, pruned after 7 days.
            con.execute('UPDATE spider_posts SET processed=1 WHERE processed=0 AND created<?',(time.time()-86400,))
            pending=con.execute('SELECT COUNT(*) FROM spider_posts WHERE processed=0').fetchone()[0]
            if pending+len(posts)>500:raise ValueError('Local queue full; let crawler catch up')
            for p in posts:
                added+=con.execute('INSERT OR IGNORE INTO spider_posts(id,created,payload) VALUES (?,?,?)',(p['id'],p['timestamp'],json.dumps(p))).rowcount
            con.execute("INSERT OR REPLACE INTO spider_meta VALUES ('last_capture',?)",(str(time.time()),))
            con.execute('DELETE FROM spider_posts WHERE created<? AND processed=1',(time.time()-7*86400,))
        return {'accepted':added,'duplicates':len(posts)-added}

    def status(self):
        with self.connect() as con:
            total=con.execute('SELECT COUNT(*) FROM spider_posts').fetchone()[0]
            pending=con.execute('SELECT COUNT(*) FROM spider_posts WHERE processed=0').fetchone()[0]
            stamp=con.execute("SELECT value FROM spider_meta WHERE key='last_capture'").fetchone()
        return {'captured':total,'pending':pending,'last_capture':float(stamp[0]) if stamp else None}

    def pending(self):
        with self.connect() as con:
            return [json.loads(r['payload']) for r in con.execute('SELECT payload FROM spider_posts WHERE processed=0 ORDER BY created LIMIT 20')]

    def ack(self,posts):
        with self.connect() as con:
            con.executemany('UPDATE spider_posts SET processed=1 WHERE id=?',[(p['id'],) for p in posts])

    def narratives(self):
        with self.connect() as con:
            posts=[json.loads(r['payload']) for r in con.execute('SELECT payload FROM spider_posts WHERE created>? ORDER BY created DESC LIMIT 500',(time.time()-86400,))]
        projects=[]
        for topic,regex in TOPICS.items():
            group=[p for p in posts if re.search(regex,p['text'],re.I)]
            if group:projects.append(candidate(group,'topic',topic,'spider-'+topic,topic.title()+' / X narrative'))
        for kind,keys in (('ticker',cashtags),('contract',solana_addresses)):
            groups={}
            for p in posts:
                for key in keys(p['text']):groups.setdefault(key,[]).append(p)
            ranked=sorted(groups.items(),key=lambda kv:(-len({p['author'].lower() for p in kv[1]}),-len(kv[1]),kv[0]))[:MAX_PER_KIND]
            for key,group in ranked:
                if kind=='ticker':projects.append(candidate(group,kind,key,'spider-ticker-'+key,f'${key} / X cashtag'))
                else:projects.append(candidate(group,kind,key,'spider-ca-'+key,f'{key[:4]}…{key[-4:]} / Solana address'))
        for phrase,group in emerging_phrases(posts):
            ident='spider-phrase-'+hashlib.sha256(phrase.encode()).hexdigest()[:12]
            projects.append(candidate(group,'phrase',phrase,ident,f'“{phrase}” / emerging narrative'))
        # Strongest leads first, so the shared Grok allowance is spent on them.
        projects.sort(key=lambda p:(-p['score'],-p['signals']['authors'],-(p['signals']['growth'] or 0)))
        return projects


MAX_PER_KIND=10
WINDOW_RECENT=6*3600
WINDOW_PREVIOUS=18*3600
# Majors and fiat are everywhere; they never mark an early narrative.
COMMON_CASHTAGS={'BTC','ETH','SOL','USD','USDT','USDC','BNB','XRP','EUR','DOGE','SPX','SPY','QQQ','NVDA','TSLA','AAPL'}
CASHTAG=re.compile(r'(?<![\w$])\$([A-Za-z][A-Za-z0-9]{1,9})(?![\w$])')
BASE58='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
ADDRESS=re.compile(r'(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])')
WORD=re.compile(r'[a-zа-яё][a-zа-яё0-9]{2,}')
STOPWORDS=set('''the and for you your this that with from have has are was were will just not but all can out about what when who how they them their our its into than then more most very new now get got one like been being also only over some any here there would could should these those which while where make made much many just today tomorrow week year time post thread link check read
это как что все для или так уже еще ещё был была были его она они мне нас вам вас там тут при над под без про чем чтобы когда если только очень сейчас сегодня завтра тоже будет есть нет него неё ней себя свой своя свои этот эта эти тот'''.split())
RISK=re.compile(r'seed phrase|private key|guaranteed profit|connect wallet to claim',re.I)


def cashtags(text):
    return {m.upper() for m in CASHTAG.findall(text)}-COMMON_CASHTAGS


def is_solana_address(value):
    """A real address decodes from base58 to exactly 32 bytes."""
    number=0
    for char in value:
        number=number*58+BASE58.index(char)
    zeros=len(value)-len(value.lstrip('1'))
    return zeros+(number.bit_length()+7)//8==32


def solana_addresses(text):
    return {m for m in ADDRESS.findall(text) if is_solana_address(m)}


def phrases(text):
    """Adjacent word pairs mapped to their position; a stopword breaks the pair."""
    text=re.sub(r'https?://\S+|[@$#]\w+','',text.lower())
    words=WORD.findall(text)
    found={}
    for i,pair in enumerate(zip(words,words[1:])):
        if not STOPWORDS.intersection(pair):found.setdefault(' '.join(pair),i)
    return found


def emerging_phrases(posts,now=None,min_authors=3,limit=5):
    """Two-word phrases repeated by distinct authors in the last 6h, outside every known topic."""
    now=now or time.time()
    recent=[p for p in posts if now-p['timestamp']<WINDOW_RECENT]
    groups={}
    for p in recent:
        for phrase in phrases(p['text']):groups.setdefault(phrase,[]).append(p)
    found=[]
    for phrase,group in groups.items():
        authors={p['author'].lower() for p in group}
        if len(authors)<min_authors or any(re.search(regex,phrase,re.I) for regex in TOPICS.values()):continue
        # Mostly inside one known topic means it is that topic's wording, not a new narrative.
        if any(sum(bool(re.search(regex,p['text'],re.I)) for p in group)*2>len(group) for regex in TOPICS.values()):continue
        # Earlier in the post usually names the thing; later pairs are commentary.
        position=sum(phrases(p['text'])[phrase] for p in group)/len(group)
        found.append((phrase,len(authors),group,position))
    found.sort(key=lambda f:(-f[1],-len(f[2]),f[3],f[0]))
    picked=[]
    for phrase,_,group,_ in found:
        # Overlapping bigrams from one sentence are one narrative, not several.
        ids={p['id'] for p in group}
        if any(ids<= {p['id'] for p in g} or {p['id'] for p in g}<=ids for _,g in picked):continue
        picked.append((phrase,[p for p in posts if phrase in phrases(p['text'])]))
        if len(picked)==limit:break
    return picked


def velocity(group,now=None):
    """Mentions in the last 6h against the hourly pace of the 18h before them."""
    now=now or time.time()
    recent=[p for p in group if now-p['timestamp']<WINDOW_RECENT]
    previous=[p for p in group if WINDOW_RECENT<=now-p['timestamp']<WINDOW_RECENT+WINDOW_PREVIOUS]
    growth=round((len(recent)/6)/(len(previous)/18),1) if previous else None
    return {'recent':len(recent),'previous':len(previous),'growth':growth,'growth_window':'6h vs prev 18h',
            'recent_authors':len({p['author'].lower() for p in recent}),
            'first_seen':min(p.get('captured_at',p['timestamp']) for p in group),
            'first_posted':min(p['timestamp'] for p in group)}


def candidate(group,kind,key,ident,name):
    group=sorted(group,key=lambda p:p['timestamp'],reverse=True)
    authors={p['author'].lower() for p in group}
    # Remove URLs and normalize punctuation to detect repeated promotional text.
    texts={re.sub(r'https?://\S+|[^\w\s]','',p['text'].lower()).strip() for p in group}
    duplicate=1-len(texts)/len(group)
    risks=any(RISK.search(p['text']) for p in group)
    checks=[('lookout',len(authors)>=4,f'{len(authors)} distinct visible authors. This is a sample of the open tab, not the whole X feed.'),
            ('maker',any(p['links'] for p in group),'External project links observed; inspect crawler evidence for product claims.'),
            ('skeptic',duplicate<=.35 and not risks,f'{duplicate:.0%} repeated text. Account authenticity is unverified.'),
            ('runner',len(group)>=5,f'{len(group)} captured posts within 24h; enough for a research lead, not an investment decision.')]
    votes=[{'seat':s,'vote':'reject' if s=='skeptic' and risks else 'pass' if ok else 'hold','reason':reason} for s,ok,reason in checks]
    status='rejected' if risks else 'shortlisted' if all(c[1] for c in checks) else 'held'
    result={'id':ident,'name':name,'url':group[-1]['url'],'source':'spider','kind':kind,'key':key,
            'status':status,'score':sum(c[1] for c in checks)*25,'votes':votes,'observed_at':group[0]['timestamp'],
            'updated':datetime.now(timezone.utc).isoformat(),
            'signals':{'mentions':len(group),'authors':len(authors),'duplicate_ratio':duplicate,**velocity(group)},
            'evidence':{'synthetic':False,'errors':[],'pages':[{'url':p['url'],'kind':'captured post','text':p['text'],'fetched_at':p['captured_at']} for p in group[:8]]},
            'posts':group[:20]}
    if kind=='topic':result['topic']=key
    else:result['research_only']=True
    return result
