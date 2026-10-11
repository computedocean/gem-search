(() => {
  if(globalThis.__gemContent){globalThis.__gemContent.start();return;}
  let ui=null,timer=null,active=false,busy=false,read=0,autoScroll=false;
  const seen=new Set();
  function stop(){active=false;clearInterval(timer);timer=null;if(ui)ui.update({paused:true,status:'Paused. Nothing is being collected.'});}
  function close(){stop();ui?.destroy();ui=null;}
  function start(){
    if(!GemExtract.supported(location.href))return;
    if(!ui)ui=new GemSpiderUI({onPause:()=>active?stop():start(),onClose:close});
    active=true;ui.update({paused:false,status:'Exploring visible posts. You control the scroll.'});
    if(!timer)timer=setInterval(tick,2800);tick();
  }
  async function tick(){
    if(!GemExtract.supported(location.href)){close();return;}
    if(!active||busy||document.hidden)return;
    const articles=[...document.querySelectorAll('article[data-testid="tweet"]')];
    const found=articles.map(el=>({el,rect:el.getBoundingClientRect()})).filter(x=>x.rect.top>=-80 && x.rect.top<innerHeight-100 && x.rect.bottom>100 && x.rect.width>100).map(x=>({...x,post:GemExtract.extract(x.el)})).find(x=>x.post&&!seen.has(x.post.id));
    if(!found){
      const typing=document.activeElement?.matches('input,textarea,[contenteditable="true"]');
      if(autoScroll&&!typing)window.scrollBy({top:Math.round(innerHeight*.6),behavior:'smooth'});
      return;
    }
    busy=true;ui.go(found.rect,'Inspecting this post…');
    try{
      const response=await chrome.runtime.sendMessage({type:'capture',posts:[found.post]});
      if(response?.ok){seen.add(found.post.id);if(seen.size>2000)seen.delete(seen.values().next().value);read++;ui?.update({read,status:response.queued?'Saved locally. Waiting for the research engine.':'Sent to the research engine. Check the dashboard for crawler and Grok status.'});}
      else ui?.update({status:response?.error||'Open the extension popup to connect.'});
    }catch{ui?.update({status:'Extension restarted. Refresh this tab to reconnect.'});stop();}
    finally{busy=false;}
  }
  chrome.runtime.onMessage.addListener((message,_sender,reply)=>{
    if(message.type==='configure'){autoScroll=message.autoScroll===true;ui?.update({status:autoScroll?'Auto-scroll on while this tab is visible. Pause any time.':'You control the scroll. Reading visible posts only.'});reply({ok:true});}
    if(message.type==='stop'){stop();reply({ok:true});}
    if(message.type==='start'){start();reply({ok:true});}
    if(message.type==='status'){reply({active,read});}
    if(message.type==='engine-status'){ui?.update({leads:message.leads,status:message.status});}
  });
  addEventListener('pagehide',close,{once:true});
  globalThis.__gemContent={start,stop};start();
})();
