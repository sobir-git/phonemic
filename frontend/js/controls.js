let dictationSerial=0;
function dictationCommand(action){
  return new Promise((resolve,reject)=>{
    if(!ws||ws.readyState!==1){ reject(new Error('Computer disconnected')); return; }
    if(dictationWait){ reject(new Error('Dictation command already pending')); return; }
    const id=++dictationSerial,socket=ws;diagnostic('dictation-request',{action,request_id:id});
    const timer=setTimeout(()=>{
      if(dictationWait?.id!==id)return;dictationWait=null;diagnostic('dictation-timeout',{action,request_id:id});
      if(socket.readyState===1)socket.send(JSON.stringify({dictation:'abort',id:++dictationSerial}));
      reject(new Error('Laptop dictation timed out. The desktop is still connected.'));
    },action==='stop'?30000:10000);
    dictationWait={id,resolve:m=>{clearTimeout(timer);resolve(m)},reject:e=>{clearTimeout(timer);reject(e)}};
    ws.send(JSON.stringify({id,dictation:action}));
  });
}
async function end(){
  if(endingPromise)return endingPromise;
  endingPromise=(async()=>{
    diagnostic('recording-release');
    if(starting){ micOff(true); paint(); return; }
    if(!talking) return;
    let audioComplete=true;
    if(audioCodec==='opus'){
      micOff(true);audioComplete=await finishOpus();
    }else flushAudio();
    talking=false; micOff(); paint(); say('Ready — the mic is off');
    if(dictation.checked&&audioComplete){
      starting=true; paint(); say('Sending to laptop dictation…');
      dictationCommand('stop').then(()=>say('Sent to laptop for transcription'))
        .catch(e=>say(e.message,'warn')).finally(()=>{starting=false;paint();});
    }else if(dictation.checked) say('Audio did not reach the laptop','warn');
  })();
  try{return await endingPromise}finally{endingPromise=null;}
}
function teardown(msg,c){
  diagnostic('connection-teardown');flushDebug();
  resetTrackpad();
  if(mousePending){mousePending.reject(new Error('Computer disconnected'));mousePending=null;}
  pressed=false; ready=false; connecting=false; talking=false; micOff();
  outputGeneration++;if(outputPane)$('output-error').textContent='Disconnected — showing the last screen';
  if(dictationWait){ dictationWait.reject(new Error("Computer disconnected")); dictationWait=null; }
  for(const pending of herdrPending.values()) pending.reject(new Error('Computer disconnected'));
  herdrPending.clear();
  for(const pending of desktopPending.values()){clearTimeout(pending.timer);pending.reject(new Error('Computer disconnected'));}
  desktopPending.clear();
  try{opusEncoder&&opusEncoder.close()}catch(e){} opusEncoder=null;opusEnding=false;opusReady=false;opusStream=0;
  if(capabilityWait){clearTimeout(capabilityWait.timer);const pending=capabilityWait;capabilityWait=null;pending.reject(Error('Computer disconnected'));}
  if(audioReadyWait){clearTimeout(audioReadyWait.timer);const pending=audioReadyWait;audioReadyWait=null;pending.reject(Error('Computer disconnected'));}
  if(audioEndWait){clearTimeout(audioEndWait.timer);const pending=audioEndWait;audioEndWait=null;pending.reject(Error('Computer disconnected'));}
  const oldSocket=ws; ws=null;
  try{oldSocket&&oldSocket.close()}catch(e){} try{ctx&&ctx.close()}catch(e){}
  ctx=null; node=null;
  try{lock&&lock.release()}catch(e){} lock=null;
  lap.textContent=''; say(msg||'Disconnected',c); paint();scheduleReconnect();
}

const trackpadPanel=$('trackpad-panel'),trackpadPad=$('trackpad-pad');
let mousePending=null,mousePointer=null,mouseDX=0,mouseDY=0,mouseClicks=[],mouseSending=false;
function resetTrackpad(){mousePointer=null;mouseDX=mouseDY=0;mouseClicks=[];}
function mouseCommand(command){
  return new Promise((resolve,reject)=>{
    if(!ready||!ws||ws.readyState!==1){reject(new Error('Disconnected. Close and reopen the trackpad.'));return;}
    const timer=setTimeout(()=>{mousePending=null;reject(new Error('Trackpad response delayed. Try again.'));},10000);
    mousePending={resolve:()=>{clearTimeout(timer);resolve()},reject:e=>{clearTimeout(timer);reject(e)}};
    ws.send(JSON.stringify({mouse:command}));
  });
}
async function flushMouse(){
  if(mouseSending||trackpadPanel.hidden)return;
  mouseSending=true;
  try{
    while(!trackpadPanel.hidden&&(mouseDX||mouseDY||mouseClicks.length)){
      if(mouseDX||mouseDY){
        const dx=Math.max(-500,Math.min(500,mouseDX)),dy=Math.max(-500,Math.min(500,mouseDY));
        mouseDX-=dx;mouseDY-=dy;await mouseCommand({action:'move',dx,dy});
      }else await mouseCommand({action:'click',button:mouseClicks.shift()});
    }
  }catch(e){resetTrackpad();$('trackpad-status').textContent=e.message;}
  finally{mouseSending=false;}
}
function mouseClick(button){if(mouseClicks.length<4){mouseClicks.push(button);flushMouse();}}
$('open-trackpad').onclick=async()=>{
  trackpadPanel.hidden=!trackpadPanel.hidden;
  $('open-trackpad').setAttribute('aria-expanded',String(!trackpadPanel.hidden));
  resetTrackpad();
  if(trackpadPanel.hidden)return;
  $('trackpad-status').textContent='Connecting…';
  if(await connect())$('trackpad-status').textContent='Slide to move. Tap to click.';
  else $('trackpad-status').textContent='Could not connect. Close and try again.';
};
$('trackpad-left').onclick=()=>mouseClick('left');
$('trackpad-right').onclick=()=>mouseClick('right');
trackpadPad.addEventListener('contextmenu',e=>e.preventDefault());
trackpadPad.addEventListener('pointerdown',e=>{
  e.preventDefault();
  if(!e.isPrimary){if(mousePointer)mousePointer.moved=true;return;}
  if(!ready)return;
  trackpadPad.setPointerCapture(e.pointerId);
  mousePointer={id:e.pointerId,x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY,time:performance.now(),moved:false};
});
trackpadPad.addEventListener('pointermove',e=>{
  if(!mousePointer||mousePointer.id!==e.pointerId)return;
  const p=mousePointer;
  if(Math.hypot(e.clientX-p.startX,e.clientY-p.startY)>6)p.moved=true;
  if(p.moved){mouseDX+=Math.round((e.clientX-p.x)*1.5);mouseDY+=Math.round((e.clientY-p.y)*1.5);}
  p.x=e.clientX;p.y=e.clientY;
  flushMouse();
});
trackpadPad.addEventListener('pointerup',e=>{
  if(!mousePointer||mousePointer.id!==e.pointerId)return;
  const tap=!mousePointer.moved&&performance.now()-mousePointer.time<350;
  mousePointer=null;if(tap)mouseClick('left');
});
trackpadPad.addEventListener('pointercancel',resetTrackpad);
trackpadPad.addEventListener('lostpointercapture',()=>{mousePointer=null;});
document.addEventListener('visibilitychange',()=>{if(document.hidden)resetTrackpad();});

function paintRemote(){
  const busy=starting||talking||capturing||remoteBusy;
  paneSelect.disabled=refreshPanes.disabled=busy;
  $('insert-text').disabled=busy||!activeDesktop?.id||(activeDesktop.herdr&&!paneSelect.value);
  $('new-workspace').disabled=$('save-workspace').disabled=busy;
  for(const button of [...remoteButtons,...document.querySelectorAll('#command-buttons button')]){
    if(!button)continue;
    button.disabled=busy||!activeDesktop?.id||(activeDesktop.herdr&&!paneSelect.value);
    if(button.dataset?.mod) button.setAttribute('aria-pressed',String(modifiers.has(button.dataset.mod)));
  }
}
async function herdrCommand(command){
  if(!await connect()) throw new Error('Could not reach the computer');
  return new Promise((resolve,reject)=>{
    const id=++herdrSerial;
    const timer=setTimeout(()=>{herdrPending.delete(id);reject(new Error('Herdr did not respond'))},5000);
    herdrPending.set(id,{resolve:v=>{clearTimeout(timer);resolve(v)},reject:e=>{clearTimeout(timer);reject(e)}});
    ws.send(JSON.stringify({id,herdr:command,window:activeDesktop?.id}));
  });
}
async function remoteAction(command){
  if(remoteBusy||starting||talking||capturing) return;
  remoteBusy=command.action;diagnostic('control-request',{action:command.action});paintRemote();
  try{
    const common=['key','scroll','text','command'].includes(command.action);
    const result=common?await desktopCommand({action:'input',command,window:activeDesktop?.id,herdr:!!activeDesktop?.herdr}):await herdrCommand(command);
    if(command.action==='list'){
      applyHerdrInventory(result);
      remoteStatus.textContent=result.panes.length?'':'No panes open in Herdr';
    }else if(command.action==='focus'){
      paneSelect.value=command.pane;renderPicked();picker.close();
      remoteStatus.textContent='';
    }else{
      remoteStatus.textContent='';
    }
    return result;
  }catch(error){
    if(command.action==='focus'){renderPicked();$('picker-summary').textContent=error.message;}
    diagnostic('control-error',debugError(error));remoteStatus.textContent=error.message;
  }finally{remoteBusy=false;paintRemote();if(command.action==='focus'||command.action==='scroll'||command.action==='key') refreshOutput(true);if(queuedPane){const id=queuedPane;queuedPane=null;choosePane(id)}}
}
refreshPanes.onclick=()=>remoteAction({action:'list'});
function setOutputOpen(open,refresh){
  $('output-home').hidden=!open;
  $('toggle-output').textContent=open?'Hide output':'Show output';
  $('toggle-output').setAttribute('aria-expanded',String(open));
  if(open){ if(refresh)refreshOutput(true); } else outputGeneration++;
}
$('toggle-output').onclick=()=>{
  const open=$('output-home').hidden;
  try{localStorage.setItem('pm.output',open?'1':'0')}catch(e){}
  setOutputOpen(open,true);
};
try{ if(localStorage.getItem('pm.output')==='1') setOutputOpen(true,false); }catch(e){}
function haptic(duration=10){try{navigator.vibrate?.(duration);}catch{}}
document.addEventListener('pointerdown',e=>{
  const button=e.target.closest?.('button,summary');
  if(button&&!button.disabled&&button!==talk&&e.isPrimary!==false)haptic();
},{passive:true});

const appsDialog=$('apps-dialog'),appsGrid=$('apps-grid'),appsStatus=$('apps-status'),appsPreviews=$('apps-previews');
let activeDesktop=null;
const desktopPending=new Map();let desktopSerial=0,appsTimer=null,appsBusy=false,appFocusBusy=false;
let appsGeneration=0,appsInventory=null,appsPreviewObserver=null,appsPreviewQueue=[],appsPreviewRunning=false,appsPreviewsEnabled=true;
try{appsPreviewsEnabled=localStorage.getItem('pm.apps.previews')!=='0';}catch{}
appsPreviews.setAttribute('aria-pressed',String(appsPreviewsEnabled));
function applyDesktopContext(context){
 const changed=activeDesktop?.id!==context?.id||activeDesktop?.herdr!==context?.herdr;
 activeDesktop=context;
 if(changed){modifiers.clear();resetTrackpad();}
 const isHerdr=!!context?.herdr;
 $('active-app-label').textContent=isHerdr?'Herdr':context?.app||'No focused window';
 paneSelect.hidden=refreshPanes.hidden=$('toggle-output').hidden=$('commands-tab').hidden=!isHerdr;
 if(!isHerdr){$('command-panel').hidden=true;$('commands-tab').setAttribute('aria-expanded','false');$('output-home').hidden=true;if(outputDialog.open)outputDialog.close();if(picker.open)picker.close();}
 $('insert-text').textContent=isHerdr?'Type into pane':'Type into app';
 paintRemote();
}
async function desktopCommand(command){
 diagnostic('desktop-request',{action:command.action==='input'?command.command?.action:command.action});
 await connect();if(!ready||!ws||ws.readyState!==1)throw Error('Computer is disconnected.');
 const id=++desktopSerial;
 return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{desktopPending.delete(id);reject(Error('Desktop request timed out.'));},10000);
  desktopPending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({desktop:command,id}));
 });
}
function stopAppPreviewWork(){
 clearTimeout(appsTimer);appsPreviewQueue=[];
 if(appsPreviewObserver){appsPreviewObserver.disconnect();appsPreviewObserver=null;}
 for(const card of appsGrid.children)if(card.dataset.previewState==='queued')card.dataset.previewState='idle';
}
function appCard(windowId){return Array.from(appsGrid.children).find(card=>card.dataset.window===windowId)||null;}
function setAppPreview(card,encoded){
 const old=card.querySelector('.app-preview');
 const preview=encoded?document.createElement('img'):document.createElement('span');
 preview.className='app-preview';preview.dataset.previewState=encoded?'ready':'unavailable';
 if(encoded){preview.src='data:image/jpeg;base64,'+encoded;preview.alt='';preview.onerror=()=>{preview.replaceWith(Object.assign(document.createElement('span'),{className:'app-preview',textContent:'Preview unavailable'}));}}
 else preview.textContent='Preview unavailable';
 old?.replaceWith(preview);
}
function renderApps(result,generation){
 if(generation!==appsGeneration||!appsDialog.open)return;
 const retainedFocus=document.activeElement?.dataset?.window;
 const existing=new Map(Array.from(appsGrid.children).map(card=>[card.dataset.window,card]));
 const seen=new Set();
 for(const w of result.windows||[]){
  let card=existing.get(w.id);
  if(!card){
   card=document.createElement('button');card.className='app-card';card.dataset.window=w.id;card.dataset.previewState='idle';
   const preview=document.createElement('span');preview.className='app-preview';
   const name=document.createElement('span');name.className='app-name';
   const title=document.createElement('span');title.className='app-title';
   card.append(preview,name,title);card.onclick=()=>focusApp(card.dataset.window);
  }
  seen.add(w.id);card.setAttribute('aria-pressed',String(w.active));card.setAttribute('aria-label',w.app+': '+w.title);
  card.querySelector('.app-name').textContent=w.app;card.querySelector('.app-title').textContent=w.title;
  if(w.preview)setAppPreview(card,w.preview);
  else if(!appsPreviewsEnabled||result.previewSupported===false){setAppPreview(card,null);card.dataset.previewState='disabled';}
  else if(card.dataset.previewState!=='ready'&&card.dataset.previewState!=='queued'&&card.dataset.previewState!=='loading'){
   const preview=card.querySelector('.app-preview');preview.textContent='Preview loading…';preview.dataset.previewState='idle';card.dataset.previewState='idle';
  }
  appsGrid.append(card);if(retainedFocus===w.id)card.focus();
 }
 for(const card of existing.values())if(!seen.has(card.dataset.window))card.remove();
 if(retainedFocus&&!appCard(retainedFocus))appsGrid.querySelector('button')?.focus();
}
function queueAppPreview(windowId,generation){
 if(!appsPreviewsEnabled||generation!==appsGeneration||!appsInventory?.previewSupported||!appCard(windowId))return;
 const card=appCard(windowId);if(['queued','loading','ready','disabled'].includes(card.dataset.previewState))return;
 card.dataset.previewState='queued';appsPreviewQueue.push({windowId,generation});drainAppPreviews();
}
async function drainAppPreviews(){
 if(appsPreviewRunning)return;appsPreviewRunning=true;
 try{while(appsPreviewQueue.length){
  const job=appsPreviewQueue.shift(),card=appCard(job.windowId);
  if(!card||job.generation!==appsGeneration||!appsDialog.open||document.hidden||!appsPreviewsEnabled)continue;
  card.dataset.previewState='loading';
  try{const result=await desktopCommand({action:'preview',window:job.windowId,snapshot:appsInventory.snapshot});
   if(job.generation!==appsGeneration||!appsDialog.open||result.snapshot!==appsInventory?.snapshot)continue;
   setAppPreview(card,result.preview);card.dataset.previewState=result.preview?'ready':'unavailable';
  }catch(error){if(job.generation===appsGeneration&&appCard(job.windowId)){card.dataset.previewState='unavailable';setAppPreview(card,null);}}
 }}finally{appsPreviewRunning=false;}
}
function watchAppPreviews(generation){
 if(!appsPreviewsEnabled||!appsInventory?.previewSupported)return;
 if(typeof IntersectionObserver==='function'){
  appsPreviewObserver=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting)queueAppPreview(entry.target.dataset.window,generation);},{root:appsDialog,rootMargin:'80px'});
  for(const card of appsGrid.children)appsPreviewObserver.observe(card);
 }else for(const card of Array.from(appsGrid.children).slice(0,4))queueAppPreview(card.dataset.window,generation);
}
async function focusApp(windowId){
 if(appFocusBusy)return;appFocusBusy=true;const generation=++appsGeneration;stopAppPreviewWork();appsStatus.textContent='Switching…';
 try{const selected=await desktopCommand({action:'focus',window:windowId});
  if(generation!==appsGeneration||!appsDialog.open)return;
  if(selected.context)applyDesktopContext(selected.context);
  const focused=selected.context?.id||windowId;for(const button of appsGrid.children)button.setAttribute('aria-pressed',String(button.dataset.window===focused));
  appsInventory=null;appsStatus.textContent='Window focused.';
 }catch(error){if(generation===appsGeneration&&appsDialog.open)appsStatus.textContent=error.message;}
 finally{if(generation===appsGeneration){appFocusBusy=false;if(appsDialog.open&&!document.hidden)appsTimer=setTimeout(()=>updateApps(),500);}}
}
