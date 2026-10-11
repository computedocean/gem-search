import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import '../extension/shared.js';
const {supported,normalize,extract}=globalThis.GemExtract;
test('capture only X feed routes, never DMs or account settings',()=>{
  assert.ok(supported('https://x.com/home'));
  assert.ok(supported('https://x.com/search?q=agents'));
  for(const url of ['https://x.com/messages/123','https://x.com/i/chat','https://x.com/settings','https://x.com.evil.test/home','https://example.com','http://x.com/home'])assert.equal(supported(url),false);
});
test('normalize posts without cookies, HTML or unrelated fields',()=>{
  const p=normalize({url:'https://x.com/researcher/status/123?track=1',text:'  Agent release  ',created_at:'2026-01-01T12:00:00Z',cookie:'do-not-copy',links:['https://docs.example.com','https://docs.example.com','javascript:alert(1)','https://t.co/private','https://x.com/status/123']});
  assert.equal(p.id,'123');assert.equal(p.text,'Agent release');assert.equal(p.url,'https://x.com/researcher/status/123');assert.deepEqual(p.links,['https://docs.example.com']);assert.equal(p.cookie,undefined);
  assert.equal(normalize({...p,created_at:'invalid'}),null);
  assert.equal(normalize({...p,url:'https://other.example/a/status/123'}),null);
});
test('extract timestamp permalink instead of quoted post permalink',()=>{
  const node={querySelector:q=>q.includes('tweetText')?{innerText:'A real research lead'}:{closest:()=>({href:'https://x.com/author/status/456'}),getAttribute:()=> '2026-01-01T12:00:00Z'},querySelectorAll:()=>[{href:'https://project.example/docs'},{href:'https://x.com/quote/status/999'}]};
  assert.equal(extract(node).id,'456');assert.deepEqual(extract(node).links,['https://project.example/docs']);
  assert.equal(extract({querySelector:()=>null}),null);
});
test('resolve t.co only when the DOM explicitly exposes the expanded URL',()=>{
  const anchor=(href,attrs={},text='')=>({href,innerText:text,textContent:text,getAttribute:key=>attrs[key]||null,dataset:{}});
  const node={querySelector:q=>q.includes('tweetText')?{innerText:'A real research lead'}:{closest:()=>({href:'https://x.com/author/status/456'}),getAttribute:()=> '2026-01-01T12:00:00Z'},querySelectorAll:()=>[
    anchor('https://t.co/abc',{'data-expanded-url':'https://project.example/docs'}),
    anchor('https://t.co/unknown',{},'Click this'),
    anchor('https://t.co/visible','', 'https://docs.example.org/guide'),
    anchor('https://x.com/quote/status/999')
  ]};
  assert.deepEqual(extract(node).links,['https://project.example/docs','https://docs.example.org/guide']);
});
test('MV3 uses explicit activation and localhost-only host permissions',()=>{
  const m=JSON.parse(fs.readFileSync(new URL('../extension/manifest.json',import.meta.url)));
  assert.equal(m.manifest_version,3);assert.deepEqual(m.host_permissions,['http://127.0.0.1/*']);assert.equal(m.content_scripts,undefined);assert.equal(m.permissions.includes('cookies'),false);
  for(const p of Object.values(m.icons))assert.ok(fs.existsSync(new URL('../extension/'+p,import.meta.url)));
});
