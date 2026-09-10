
const $=i=>document.getElementById(i);
const talk=$('talk'),st=$('st'),lap=$('lap'),hf=$('hf'),dis=$('dis'),q=$('q'),transport=$('transport'),
      gear=$('gear'),panel=$('panel'),vis=$('vis'),g=vis.getContext('2d'),dictation=$('dictation');
let ws,ctx,node,src,stream,lock=null;
let ready=false,talking=false,connecting=false,gen=0,pressed=false,starting=false;
let dictationWait=null, audioInit=null, capturing=false;
let reconnectTimer=null,reconnectDelay=1000,manualDisconnect=false,lastMessageAt=0;
let audioCodec='pcm',audioFraming='pcm',audioCaps=null,capabilityWait=null,audioReadyWait=null,audioEndWait=null,audioNegotiationId=0;
let opusEncoder=null,opusPending=[],opusPendingN=0,opusStream=0,opusSeq=0,opusTimestamp=0;
let opusFailed=false,opusEnding=false,opusReady=false,endingPromise=null,wireSent=0;
const paneSelect=$('panes'),remoteStatus=$('remote-status'),refreshPanes=$('refresh-panes');
const remoteButtons=Array.from(document.querySelectorAll('#remote [data-key],#remote [data-mod],#remote [data-scroll],#remote [data-pane-action]'));
let inventory=[],pickerView='agents',workspaceFilter=null,queuedPane=null;
const picker=$('pane-picker');
const outputViewer=$('output-viewer'),outputScroll=$('output-scroll'),terminalOutput=$('terminal-output');
const outputDialog=$('output-dialog'),followOutput=$('follow-output');
let outputPane='',outputText=null,outputBusy=false,outputFollow=true,outputGeneration=0;
let herdrPending=new Map(),herdrSerial=0,remoteBusy=false,modifiers=new Set(),connectionPromise=null;

// Bounded metadata only. Never record text, commands, audio, URLs, or credentials.
let debugQueue=[],debugTimer=null,debugSending=false,debugSeq=0,debugRx=0;
const debugClient=Math.random().toString(36).slice(2,10);
try{const saved=JSON.parse(sessionStorage.getItem('pm.debug.events')||'[]');if(Array.isArray(saved))debugQueue=saved.slice(-60);}catch{}
function saveDebug(){try{sessionStorage.setItem('pm.debug.events',JSON.stringify(debugQueue));}catch{}}
function debugError(error){return {error:error?.name||'Error'};}
function diagnostic(event,extra={}){
 if(typeof fetch==='undefined')return;
 const entry={event,client:debugClient,seq:++debugSeq,time_ms:Date.now(),...extra};
 try{Object.assign(entry,{ready,starting,talking,capturing,hidden:!!document.hidden,online:navigator.onLine!==false,
  dictation:dictation.checked,socket:ws?.readyState??3,queued_samples:pendN,buffered_bytes:ws?.bufferedAmount||0,
  sent_bytes:wireSent,received_bytes:debugRx,audio_state:ctx?.state||'none',app_mode:activeDesktop?.herdr?'herdr':activeDesktop?.id?'generic':'none'});}catch{}
 debugQueue.push(entry);debugQueue=debugQueue.slice(-60);saveDebug();
 if(!debugTimer)debugTimer=setTimeout(flushDebug,250);
}
async function flushDebug(){
 debugTimer=null;if(debugSending||!debugQueue.length||typeof fetch==='undefined')return;
 debugSending=true;const batch=debugQueue.slice(0,8);
 try{
  const response=await fetch('/diagnostics',{headers:{'X-PhoneMic-Diagnostics':JSON.stringify(batch)},cache:'no-store',keepalive:true});
  if(response.ok){const sentEntries=new Set(batch);debugQueue=debugQueue.filter(e=>!sentEntries.has(e));saveDebug();}
 }catch{}finally{debugSending=false;if(debugQueue.length&&!debugTimer)debugTimer=setTimeout(flushDebug,3000);}
}
globalThis.addEventListener?.('error',e=>diagnostic('javascript-error',{...debugError(e.error),line:e.lineno||0,column:e.colno||0}));
globalThis.addEventListener?.('unhandledrejection',e=>diagnostic('unhandled-rejection',debugError(e.reason)));
setInterval(()=>{if(starting||capturing||talking)diagnostic('recording-progress');},5000);

try{ const v=localStorage.getItem('pm.q'); if(v) q.value=v==='48000:1'?'48000:1:20':v; }catch(e){}
try{ const v=localStorage.getItem('pm.transport'); if(v==='auto'||v==='pcm') transport.value=v; }catch(e){}
try{ hf.checked = localStorage.getItem('pm.hf')==='1'; }catch(e){}
try{ dictation.checked = localStorage.getItem('pm.dictation')==='1'; }catch(e){}
// Carry only preferences across origins; each origin asks for its own mic permission.
try{
  const handoff=new URLSearchParams(location.hash.slice(1));
  if(handoff.has('pmq')){
    if(['48000:1','48000:1:20','48000:1:16','48000:0','24000:1','16000:1'].includes(handoff.get('pmq'))){const v=handoff.get('pmq');q.value=v==='48000:1'?'48000:1:20':v;}
    if(['auto','pcm'].includes(handoff.get('pmt')))transport.value=handoff.get('pmt');
    hf.checked=handoff.get('pmhf')==='1';dictation.checked=handoff.get('pmd')==='1';
    localStorage.setItem('pm.q',q.value);localStorage.setItem('pm.hf',hf.checked?'1':'0');
    localStorage.setItem('pm.dictation',dictation.checked?'1':'0');
    history.replaceState(null,'',location.pathname);
  }
}catch(e){}
function setupConnection(){
  const config=JSON.parse($('connection-config').textContent);
  if(!config.local)return;
  const local=new URL(config.local);
  const onLaptop=location.origin===local.origin;
  const target=onLaptop?config.public:config.local;
  $('connection-card').hidden=false;
  $('connection-title').textContent=onLaptop?'Direct Wi-Fi connection':'Direct over Wi-Fi';
  if(onLaptop)$('connection-help').textContent='Connected straight to your laptop over the local network. Keep both devices on the same Wi-Fi.';
  $('connection-address').textContent=local.host;
  const button=$('switch-connection');button.hidden=!target;
  button.textContent=onLaptop?'Use internet connection':'Use Wi-Fi connection';
  button.onclick=async()=>{
    if(talking||starting||capturing){$('connection-help').textContent='Finish recording before switching connections.';return;}
    try{
      const next=new URL(target);next.search='';
      const response=await fetch('/auth/handoff',{headers:{'X-PhoneMic-Target':next.origin},cache:'no-store'});
      if(!response.ok)throw Error('Pair this browser again before switching connections.');
      const {ticket}=await response.json();
      next.hash=new URLSearchParams({handoff:ticket,pmq:q.value,pmt:transport.value,pmhf:hf.checked?'1':'0',pmd:dictation.checked?'1':'0'}).toString();
      location.assign(next.href);
    }catch(error){$('connection-help').textContent=error.message;}

  };
}
setupConnection();
const quality=()=>{ const [r,pr,b]=q.value.split(':'); return {rate:+r, proc:pr==='1', bitrate:+b||0}; };
const say=(t,c)=>{st.textContent=t;const dot=document.createElement('span');dot.className='dot '+(c||'');st.prepend(dot);};
if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{});

gear.onclick=()=>{ panel.hidden=!panel.hidden; gear.classList.toggle('open',!panel.hidden); };

// ---- waveform: one bar per BAR_MS, not per render quantum (~2.7ms) ----
const BARS=140, BAR_MS=60, PER_SEC=Math.round(1000/BAR_MS);
const hist=[]; let sent=0, accPeak=0, accSent=false, accT=0;
function push(peak,sending){
  const now=performance.now();
  if(!accT) accT=now;
  if(peak>accPeak) accPeak=peak;
  if(sending) accSent=true;
  if(now-accT < BAR_MS) return;
  hist.push({p:accPeak,s:accSent?1:0,at:sent});
  while(hist.length>BARS) hist.shift();
  accPeak=0; accSent=false; accT=now;
}
function confirmRx(rx){
  if(typeof rx!=='number') return;debugRx=rx;
  for(const h of hist) if(h.s===1 && h.at<=rx) h.s=2;
}
function draw(){
  if(!talking) push(0,false);        // keep the timeline scrolling when idle
  const W=vis.width,H=vis.height,bw=W/BARS;
  g.fillStyle='#000000'; g.fillRect(0,0,W,H);
  g.fillStyle='#232C46';
  for(let k=PER_SEC;k<BARS;k+=PER_SEC) g.fillRect(W-k*bw,0,1,H);
  g.fillStyle='#4E5C92'; g.fillRect(0,H/2-1,W,2);
  for(let i=0;i<hist.length;i++){
    const h=hist[i],x=W-(hist.length-i)*bw;
    const amp=Math.max(2,Math.min(1,h.p*1.5)*(H*0.9));
    g.fillStyle=h.s===2?'#19F0A6':h.s===1?'#FFC85C':'#4A5580';
    g.fillRect(x+bw*0.15,(H-amp)/2,Math.max(1,bw*0.7),amp);
  }
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);

function paint(){
  talk.className = (talking||capturing)?'live':starting?'busy':'';
  dictation.disabled=q.disabled=transport.disabled=hf.disabled=starting||talking;
  talk.textContent = (talking||capturing) ? (hf.checked?'On — tap to stop':'Recording')
    : starting?'Opening mic…':(hf.checked?'Tap to ':'Touch to ')+(dictation.checked?'dictate':'talk');
  paintRemote();
  dis.style.display = ready?'':'none';
}
function renderLaptop(m){
  if(m.mic===null){ lap.textContent='Computer: virtual microphone missing'; return; }
  lap.textContent=(m.apps&&m.apps.length)?'In use by: '+m.apps.join(', ')
                                         :'Connected. No app has selected it yet.';
}

// The socket and audio graph stay up; the microphone itself does not.
async function checkBrowserAccess(){
  if(typeof fetch==='undefined')return;
  try{const response=await fetch('/auth/status',{cache:'no-store'});
    if(response.status===401){manualDisconnect=true;clearTimeout(reconnectTimer);location.replace('/');}
  }catch{}
}
function scheduleReconnect(delay=reconnectDelay){
  if(manualDisconnect||document.hidden||navigator.onLine===false||reconnectTimer!==null)return;
  reconnectTimer=setTimeout(()=>{
    reconnectTimer=null;
    if(manualDisconnect||document.hidden||navigator.onLine===false)return;
    reconnectDelay=Math.min(reconnectDelay*2,15000);
    connect().catch(()=>{});
  },delay);
}
function resumeConnection(immediate=true){
  if(manualDisconnect||document.hidden||navigator.onLine===false)return;
  if(ready&&(!ws||ws.readyState!==1||Date.now()-lastMessageAt>15000))teardown('Reconnecting…');
  if(!ready){
    if(immediate){clearTimeout(reconnectTimer);reconnectTimer=null;scheduleReconnect(0);}
    else scheduleReconnect();
  }
}
function connect(){
  manualDisconnect=false;clearTimeout(reconnectTimer);reconnectTimer=null;
  if(connectionPromise) return connectionPromise;
  connectionPromise=connectSocket().finally(()=>{connectionPromise=null;if(!ready)scheduleReconnect()});
  return connectionPromise;
}
async function connectSocket(){
  if(ready&&ws&&ws.readyState===1) return true;
  if(connecting) return false;
  if(ready){ const oldSocket=ws; ws=null; ready=false;
    try{oldSocket&&oldSocket.close()}catch(_){} }
  connecting=true;diagnostic('connection-opening');talk.classList.add('busy');say('Connecting…');
  const Q=quality();
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
  const socket=ws;
  ws.binaryType='arraybuffer';
  ws.onmessage=e=>{ if(ws!==socket) return; lastMessageAt=Date.now(); try{ const m=JSON.parse(e.data);
    if(m.type==='audio-capabilities'){
      if(capabilityWait&&(m.id===capabilityWait.id||(m.id==null&&capabilityWait.allowLegacy))){
        const pending=capabilityWait;capabilityWait=null;clearTimeout(pending.timer);
        if(!m.error){audioCaps=m;audioCodec=m.codec==='opus'?'opus':'pcm';audioFraming=m.framing||'pcm';}
        pending.resolve(m);
      }
      return;
    }
    if(m.type==='audio-ready'){
      if(audioReadyWait&&m.stream===audioReadyWait.stream){const pending=audioReadyWait;audioReadyWait=null;clearTimeout(pending.timer);m.error?pending.reject(Error(m.error)):pending.resolve(m)}
      return;
    }
    if(m.type==='audio-ended'){
      if(audioEndWait&&m.stream===audioEndWait.stream){const pending=audioEndWait;audioEndWait=null;clearTimeout(pending.timer);m.error?pending.reject(Error(m.error)):pending.resolve(m)}
      return;
    }
    if(m.type==='desktop-context'){applyDesktopContext(m.result);return;}
    if(m.type==='desktop'){diagnostic(m.error?'desktop-error':'desktop-ready',{request_id:m.id||0});const p=desktopPending.get(m.id);if(p){desktopPending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(Error(m.error)):p.resolve(m.result);}return;}
    if(m.type==='mouse'){
      if(mousePending){const pending=mousePending;mousePending=null;m.error?pending.reject(new Error(m.error)):pending.resolve();}
      return;
    }
    if(m.type==='herdr-update'){ applyHerdrInventory(m.result); return; }
    if(m.type==='herdr'){
      const pending=herdrPending.get(m.id);
      if(pending){herdrPending.delete(m.id);m.error?pending.reject(new Error(m.error)):pending.resolve(m.result)}
      return;
    }
    if(m.type==='dictation'){
      if(dictationWait){ const pending=dictationWait;
        if(m.id!==pending.id)return;
        dictationWait=null;diagnostic(m.error?'dictation-error':'dictation-ready',{action:m.action||'unknown',request_id:m.id||0});m.error?pending.reject(new Error(m.error)):pending.resolve(m); }
      return;
    }
    renderLaptop(m); confirmRx(m.rx);
  }catch(_){} };
  ws.onclose=(event={})=>{if(ws===socket){diagnostic('connection-closed',{code:event.code||0});teardown('Connection lost — reconnecting…','warn');checkBrowserAccess();}};
  ws.onerror=()=>{ if(ws===socket)diagnostic('connection-error');if(ws===socket) say('Connection error','warn'); };
  try{ await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{socket.close();reject(new Error('Connection timed out'))},10000);
    socket.onopen=()=>{clearTimeout(timer);resolve()};
    socket.addEventListener('close',()=>{clearTimeout(timer);reject(new Error('Connection closed'))},{once:true});
  });
  if(ws!==socket||socket.readyState!==1) throw new Error('Connection closed'); }
  catch(e){ if(ws===socket){connecting=false;talk.classList.remove('busy');say('Could not reach the computer — retrying…','warn');} checkBrowserAccess();return false; }
  const wantsOpus=transport.value==='auto'&&Q.bitrate>0&&typeof AudioEncoder==='function'&&typeof AudioData==='function';
  audioCodec=wantsOpus?'opus':'pcm';audioFraming=wantsOpus?'bare':'pcm';audioCaps=null;
  const negotiate=(request,allowLegacy=false)=>new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{if(capabilityWait?.id===request.id){capabilityWait=null;reject(Error('Audio negotiation timed out'))}},3000);
    capabilityWait={id:request.id,allowLegacy,timer,resolve,reject};socket.send(JSON.stringify(request));
  });
  try{
    if(wantsOpus){
      const caps=await negotiate({audio_v:3,id:++audioNegotiationId,codec:'opus',framing:'bare',rate:48000,bitrate:Q.bitrate,proc:Q.proc});
      if(caps.error||caps.codec!=='opus')throw Error(caps.error||'Audio codec was not accepted');
      if(caps.framing!=='bare')throw Error('Compact Opus framing was not accepted');
    }else socket.send(JSON.stringify({audio_v:2,id:++audioNegotiationId,codec:'pcm',rate:Q.rate,proc:Q.proc}));
  }catch(error){
    if(!wantsOpus){if(ws===socket)socket.close();return false;}
    try{
      audioCodec='pcm';audioFraming='pcm';
      socket.send(JSON.stringify({audio_v:2,id:++audioNegotiationId,codec:'pcm',rate:Q.rate,proc:Q.proc}));
    }catch(fallbackError){
      diagnostic('audio-negotiation-failed',debugError(fallbackError));if(ws===socket)socket.close();return false;
    }
  }
  if(ws!==socket||socket.readyState!==1)return false;
  ready=true; reconnectDelay=1000;lastMessageAt=Date.now();connecting=false; talk.classList.remove('busy');
  if(!starting&&!capturing&&!talking) say('Ready — touch to record');
  diagnostic('connection-ready');flushDebug();paint();return true;
}
