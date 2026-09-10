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

const appsDialog=$('apps-dialog'),appsGrid=$('apps-grid'),appsStatus=$('apps-status');
let activeDesktop=null;
const desktopPending=new Map();let desktopSerial=0,appsTimer=null,appsBusy=false,appFocusBusy=false;
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
async function updateApps(){
 clearTimeout(appsTimer);
 if(!appsDialog.open||document.hidden||appsBusy||appFocusBusy)return;
 appsBusy=true;
 try{
  const result=await desktopCommand({action:'list'});
  if(!appsDialog.open)return;
  const retainedFocus=document.activeElement?.dataset?.window;
  appsGrid.replaceChildren();
  for(const w of result.windows){
   const card=document.createElement('button');card.className='app-card';card.dataset.window=w.id;
   card.setAttribute('aria-pressed',String(w.active));card.setAttribute('aria-label',w.app+': '+w.title);
   const preview=document.createElement(w.preview?'img':'span');preview.className='app-preview';
   if(w.preview){preview.src='data:image/jpeg;base64,'+w.preview;preview.alt='';}else preview.textContent='Preview unavailable';
   const name=document.createElement('span');name.className='app-name';name.textContent=w.app;
   const title=document.createElement('span');title.className='app-title';title.textContent=w.title;
   card.append(preview,name,title);card.onclick=async()=>{
    if(appFocusBusy)return;appFocusBusy=true;clearTimeout(appsTimer);appsStatus.textContent='Switching…';
    try{const selected=await desktopCommand({action:'focus',window:w.id});if(selected.context)applyDesktopContext(selected.context);appsStatus.textContent='Window focused.';for(const button of appsGrid.children)button.setAttribute('aria-pressed',String(button.dataset.window===w.id));}
    catch(e){appsStatus.textContent=e.message;}
    finally{appFocusBusy=false;if(appsDialog.open)appsTimer=setTimeout(updateApps,3000);}
   };appsGrid.append(card);if(retainedFocus===w.id)card.focus();
  }
  appsStatus.textContent=result.windows.length?'Tap a window to switch. Previews refresh every 3 seconds.':'No application windows found.';
 }catch(e){if(appsDialog.open)appsStatus.textContent=e.message;}
 finally{appsBusy=false;if(appsDialog.open&&!document.hidden)appsTimer=setTimeout(updateApps,3000);}
}
$('open-apps').onclick=()=>{appsDialog.showModal();updateApps();};
$('apps-close').onclick=()=>appsDialog.close();
appsDialog.addEventListener('close',()=>{clearTimeout(appsTimer);appsGrid.replaceChildren();});
document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(appsTimer);else if(appsDialog.open)updateApps();});

const defaultCommands=['/clear','/model','cx','cc-yolo'];
let savedCommands=[...defaultCommands];
try{
  const current=localStorage.getItem('phonemic-command-buttons');
  const saved=JSON.parse(current??localStorage.getItem('phonemic-commands')??'[]');
  if(Array.isArray(saved)){
    const valid=saved.filter(c=>typeof c==='string'&&c.trim()&&c.length<=2000);
    savedCommands=[...new Set(current===null?[...defaultCommands,...valid]:valid)].slice(0,50);
  }
}catch{}
function renderCommands(){
  const list=$('command-buttons');const add=$('add-command');list.replaceChildren();
  for(const command of savedCommands){
    const button=document.createElement('button');button.type='button';button.textContent=command;
    let timer=null,held=false,startX=0,startY=0;
    const cancel=()=>{clearTimeout(timer);timer=null;};
    const remove=()=>{
      cancel();held=true;haptic(25);
      if(confirm('Delete command "'+command+'"?'))storeCommands(savedCommands.filter(c=>c!==command));
    };
    button.addEventListener('pointerdown',e=>{
      if(button.disabled||e.isPrimary===false||e.button!==0)return;
      cancel();held=false;startX=e.clientX;startY=e.clientY;
      timer=setTimeout(remove,600);
    });
    button.addEventListener('pointermove',e=>{if(Math.hypot(e.clientX-startX,e.clientY-startY)>10){cancel();held=true;}});
    for(const event of ['pointerup','pointerleave'])button.addEventListener(event,cancel);
    button.addEventListener('pointercancel',()=>{cancel();held=true;});
    button.addEventListener('contextmenu',e=>{e.preventDefault();if(!held&&!button.disabled)remove();});
    button.onclick=()=>{
      cancel();if(held){held=false;return;}
      remoteAction({action:'command',pane:paneSelect.value,text:command});
    };
    list.append(button);
  }
  list.append(add);
  paintRemote();
}
function storeCommands(next){
  try{localStorage.setItem('phonemic-command-buttons',JSON.stringify(next));savedCommands=next;renderCommands();return true;}
  catch{remoteStatus.textContent=$('command-error').textContent='Could not save commands in this browser.';return false;}
}
$('add-command').onclick=()=>{$('custom-command').value='';$('command-error').textContent='';$('command-dialog').showModal();$('custom-command').focus();};
$('cancel-command').onclick=()=>$('command-dialog').close();
$('save-command').onsubmit=e=>{
