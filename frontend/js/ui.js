async function updateApps(){
 clearTimeout(appsTimer);
 if(!appsDialog.open||document.hidden||appsBusy||appFocusBusy)return;
 const generation=++appsGeneration;appsBusy=true;stopAppPreviewWork();
 if(appsInventory){renderApps(appsInventory,generation);appsStatus.textContent='Updating applications…';}
 else appsStatus.textContent='Loading applications…';
 try{const result=await desktopCommand({action:'list'});
  if(generation!==appsGeneration||!appsDialog.open)return;
  appsInventory=result;renderApps(result,generation);
  appsStatus.textContent=result.windows?.length?'Tap a window to switch.':'No application windows found.';
  watchAppPreviews(generation);
 }catch(error){if(generation===appsGeneration&&appsDialog.open)appsStatus.textContent=error.message;}
 finally{if(generation===appsGeneration){appsBusy=false;if(appsDialog.open&&!document.hidden)appsTimer=setTimeout(()=>updateApps(),5000);}}
}
appsPreviews.onclick=()=>{appsPreviewsEnabled=!appsPreviewsEnabled;appsPreviews.setAttribute('aria-pressed',String(appsPreviewsEnabled));try{localStorage.setItem('pm.apps.previews',appsPreviewsEnabled?'1':'0')}catch{};const generation=++appsGeneration;stopAppPreviewWork();if(appsInventory&&appsDialog.open){renderApps(appsInventory,generation);watchAppPreviews(generation);}};
$('open-apps').onclick=()=>{appsDialog.showModal();if(appsInventory)renderApps(appsInventory,++appsGeneration);updateApps();};
$('apps-close').onclick=()=>appsDialog.close();
appsDialog.addEventListener('close',()=>{++appsGeneration;stopAppPreviewWork();appsBusy=false;appFocusBusy=false;});
document.addEventListener('visibilitychange',()=>{if(document.hidden){++appsGeneration;stopAppPreviewWork();appsBusy=false;}else if(appsDialog.open)updateApps();});

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
  e.preventDefault();const command=$('custom-command').value.trim();
  if(!command||command.length>2000)return;
  if(savedCommands.length>=50&&!savedCommands.includes(command)){$('command-error').textContent='Delete a command before adding more.';return;}
  if(storeCommands([...new Set([...savedCommands,command])]))$('command-dialog').close();
};
for(const button of document.querySelectorAll('[data-pane-action]'))button.onclick=async()=>{
  const pane=paneSelect.value,action=button.dataset.paneAction;
  if(action==='close'&&!confirm('Close this pane and end its running terminal session?'))return;
  const result=await remoteAction({action,pane});
  if(result)await remoteAction({action:'list'});
};
renderCommands();

function toggleControlPanel(name){
  const opening=$(name+'-panel').hidden;
  for(const [tab,panel] of [['commands-tab','command-panel'],['composer-tab','composer-panel']]){
    const active=opening&&panel===name+'-panel';
    $(panel).hidden=!active;$(tab).setAttribute('aria-expanded',String(active));
  }
}
$('commands-tab').onclick=()=>toggleControlPanel('command');
$('composer-tab').onclick=()=>toggleControlPanel('composer');
$('insert-text').onclick=()=>{
  const text=$('composer').value;if(text.trim())remoteAction({action:'text',pane:paneSelect.value,text});
};
$('clear-draft').onclick=()=>{$('composer').value='';saveDraft();};
function saveDraft(){ try{localStorage.setItem('pm.draft',$('composer').value)}catch(e){} }
$('composer').addEventListener('input',saveDraft);
try{ const d=localStorage.getItem('pm.draft'); if(d)$('composer').value=d; }catch(e){}
$('new-workspace').onclick=()=>{
  $('workspace-name').value='';$('workspace-directory').value='';$('workspace-error').textContent='';
  $('workspace-dialog').showModal();$('workspace-name').focus();
};
$('cancel-workspace').onclick=()=>$('workspace-dialog').close();
$('create-workspace').onsubmit=async e=>{
  e.preventDefault();const label=$('workspace-name').value.trim();if(!label)return;
  const result=await remoteAction({action:'workspace',pane:paneSelect.value||undefined,label,cwd:$('workspace-directory').value.trim()});
  if(result){$('workspace-dialog').close();await remoteAction({action:'list'});}
  else $('workspace-error').textContent=remoteStatus.textContent||'Could not create space.';
};
let completionAlerts=false;
function paintAlerts(){
  $('notifications').textContent='Completion alerts: '+(completionAlerts?'on':'off');
  $('notifications').setAttribute('aria-pressed',String(completionAlerts));
  $('notification-status').textContent=completionAlerts?'Watching while this page stays connected.':'';
}
$('notifications').onclick=async()=>{
  completionAlerts=!completionAlerts;
  try{localStorage.setItem('pm.alerts',completionAlerts?'1':'0')}catch(e){}
  paintAlerts();
  if(completionAlerts&&typeof Notification!=='undefined'&&Notification.permission==='default'){
    try{await Notification.requestPermission();}catch{}
  }
};
// Only restore "on" while the notification grant still stands, so the label
// never claims alerts the browser would drop.
try{
  completionAlerts = localStorage.getItem('pm.alerts')==='1'
    && (typeof Notification==='undefined'||Notification.permission==='granted');
}catch(e){}
paintAlerts();
function notifyCompletions(panes){
  if(!completionAlerts)return;
  for(const pane of panes){
    const previous=inventory.find(p=>p.id===pane.id);
    if(previous?.state!=='working'||!['idle','done','blocked'].includes(pane.state))continue;
    const message=pane.workspace+' · '+(pane.agent||'Agent')+(pane.state==='blocked'?' needs input':' finished');
    $('notification-status').textContent=message;
    try{navigator.vibrate?.([100,50,100]);}catch{}
    if(typeof Notification!=='undefined'&&Notification.permission==='granted'){
      try{new Notification('PhoneMic',{body:message,tag:'phonemic-'+pane.id});}catch{}
    }
  }
}
function searchOutput(){
  const query=$('search-output').value.trim().toLowerCase();
  const results=$('search-results');results.hidden=!query;
  if(!query){results.textContent='';$('search-count').textContent='';return;}
  const plain=terminalRuns(outputText||'').map(run=>run.text).join('');
  const matches=plain.split('\n').map((line,i)=>({line,number:i+1})).filter(row=>row.line.toLowerCase().includes(query));
  results.textContent=matches.map(row=>row.number+': '+row.line).join('\n');
  $('search-count').textContent=matches.length+' matching lines in this screen';
}
$('search-output').oninput=()=>{if($('search-output').value)setOutputFollow(false);searchOutput();};
const stateNames={idle:'Idle',working:'Working',done:'Done',blocked:'Needs input',unknown:'Terminal'};
function agentState(value){return Object.hasOwn(stateNames,value)?value:'unknown'}
function syncFocusedPane(panes){
  inventory=Array.isArray(panes)?panes:[];
  const focused=inventory.find(p=>p.focused);
  if(focused) paneSelect.value=focused.id;
  else if(!inventory.some(p=>p.id===paneSelect.value)) paneSelect.value='';
}
function applyHerdrInventory(result){
  if(!result||!Array.isArray(result.panes)) return;
  notifyCompletions(result.panes);
  syncFocusedPane(result.panes);
  renderPicked();if(picker.open) renderPicker();
}
function renderPicked(){
  const pane=inventory.find(p=>p.id===paneSelect.value);
  $('picked-name').textContent=pane?pane.workspace:'Choose an agent';
  $('picked-detail').textContent=pane?paneDetail(pane):'no pane selected';
  $('picked-dot').className='agent-dot '+agentState(pane?.state);
  selectOutputPane(paneSelect.value);
}
// Terminal output is untrusted: create text nodes, never HTML or clickable links.
const ansiColors=['#000000','#fa7777','#72dc99','#f2d675','#85b5ff','#d6a0f4','#73dfe4','#dbe5f3',
  '#8d9aaf','#ff9999','#98f1b5','#ffeb94','#adcfff','#e7baff','#a0f0f2','#ffffff'];
function terminalColor(index){
  if(index<16) return ansiColors[index];
  if(index<232){const n=index-16,v=[0,95,135,175,215,255];return 'rgb('+[v[Math.floor(n/36)],v[Math.floor(n/6)%6],v[n%6]].join(',')+')'}
  const v=8+(index-232)*10;return 'rgb('+[v,v,v].join(',')+')';
}
function terminalRuns(text){
  const runs=[];let style={},buffer='';
  function flush(){if(buffer){runs.push({text:buffer,style:{...style}});buffer=''}}
  function sgr(params){
    const codes=(params||'0').split(/[;:]/).map(Number);
    for(let i=0;i<codes.length;i++){
      const c=codes[i];
      if(c===0) style={};
      else if(c===1) style.bold=true;
      else if(c===22) style.bold=false;
      else if(c===4) style.underline=true;
      else if(c===24) style.underline=false;
      else if(c===7) style.reverse=true;
      else if(c===27) style.reverse=false;
      else if(c===39) delete style.fg;
      else if(c===49) delete style.bg;
      else if(c>=30&&c<=37) style.fg=ansiColors[c-30];
      else if(c>=90&&c<=97) style.fg=ansiColors[c-90+8];
      else if(c>=40&&c<=47) style.bg=ansiColors[c-40];
      else if(c>=100&&c<=107) style.bg=ansiColors[c-100+8];
      else if(c===38||c===48){
        const target=c===38?'fg':'bg';
        if(codes[i+1]===5&&Number.isInteger(codes[i+2])&&codes[i+2]>=0&&codes[i+2]<=255){style[target]=terminalColor(codes[i+2]);i+=2}
        else if(codes[i+1]===2&&codes.slice(i+2,i+5).length===3&&codes.slice(i+2,i+5).every(v=>Number.isInteger(v)&&v>=0&&v<=255)){
          style[target]='rgb('+codes.slice(i+2,i+5).join(',')+')';i+=4;
        }
      }
    }
  }
  for(let i=0;i<text.length;i++){
    const c=text.charCodeAt(i);
    if(c===27){
      flush();
      if(text[i+1]==='['){
        let end=i+2;while(end<text.length&&(text.charCodeAt(end)<64||text.charCodeAt(end)>126))end++;
        if(text[end]==='m') sgr(text.slice(i+2,end));i=end;
      }else if(text[i+1]===']'||text[i+1]==='P'){
        i+=2;while(i<text.length&&text.charCodeAt(i)!==7&&!(text.charCodeAt(i)===27&&text.charCodeAt(i+1)===92))i++;
        if(text.charCodeAt(i)===27)i++;
      }else{i++;}
    }else if(c===10||c===9||c>=32&&c!==127){buffer+=text[i];}
  }
  flush();return runs;
}
function renderTerminal(text){
  const fragment=document.createDocumentFragment();
  for(const run of terminalRuns(text)){
    const span=document.createElement('span'),style=run.style;span.textContent=run.text;
    if(style.fg)span.style.color=style.fg;
    if(style.bg)span.style.backgroundColor=style.bg;
    if(style.bold)span.style.fontWeight='700';
    if(style.underline)span.style.textDecoration='underline';
    if(style.reverse){span.style.color=style.bg||'#000000';span.style.backgroundColor=style.fg||'#e8eff9'}
    fragment.append(span);
  }
  terminalOutput.replaceChildren(fragment);
}
function setOutputFollow(value){
  outputFollow=value;followOutput.setAttribute('aria-pressed',String(value));
}
function selectOutputPane(pane){
  if(outputPane===pane)return;
  outputPane=pane;outputGeneration++;outputText=null;setOutputFollow(true);
  terminalOutput.textContent=pane?'Loading terminal…':'Select a pane to see its output.';
  $('output-error').textContent='';searchOutput();
}
async function refreshOutput(force=false){
  if($('output-home').hidden||outputBusy||!outputPane||!ready||document.hidden||starting||talking||capturing||remoteBusy||(!outputFollow&&!force))return;
  outputBusy=true;
  const pane=outputPane,generation=outputGeneration;
  try{
    const result=await herdrCommand({action:'read',pane});
    if(outputPane!==pane||generation!==outputGeneration||(!outputFollow&&!force))return;
    if(result.pane!==pane||typeof result.text!=='string')throw new Error('Invalid terminal output');
    if(result.text!==outputText){
      outputText=result.text;renderTerminal(result.text||'This pane has no output yet.');searchOutput();
    }
    if(outputFollow)outputScroll.scrollTop=outputScroll.scrollHeight;
    $('output-error').textContent=result.truncated?'Showing the available screen snapshot.':'';
  }catch(error){if(outputPane===pane&&generation===outputGeneration)$('output-error').textContent='Output unavailable: '+error.message;}
  finally{outputBusy=false;}
}
followOutput.onclick=()=>{setOutputFollow(!outputFollow);if(outputFollow)refreshOutput(true)};
// Pause on reading gestures rather than snapping the reader back to the bottom.
outputScroll.addEventListener('wheel',e=>{if(e.deltaY<0)setOutputFollow(false)},{passive:true});
outputScroll.addEventListener('touchstart',()=>setOutputFollow(false),{passive:true});
outputScroll.addEventListener('pointerdown',()=>setOutputFollow(false));
outputScroll.addEventListener('keydown',e=>{if(['ArrowUp','PageUp','Home'].includes(e.key))setOutputFollow(false)});
$('expand-output').onclick=()=>{
  if(outputDialog.open){outputDialog.close();return;}
  outputDialog.append(outputViewer);outputDialog.showModal();
  $('expand-output-icon').setAttribute('href','#icon-collapse');
  $('expand-output').setAttribute('aria-label','Close expanded output');
};
outputDialog.addEventListener('close',()=>{
  $('output-home').append(outputViewer);$('expand-output-icon').setAttribute('href','#icon-expand');
  $('expand-output').setAttribute('aria-label','Expand terminal output');
});
setInterval(()=>refreshOutput(),1000);

function paneDetail(pane){
  const agent=pane.agent||'terminal';
  const title=(pane.title||'').replace(/^[\s\u2800-\u28ff✳✻✽✶✢◐◓◑◒⏺●]+/u,'')
    .replace(/\s*[|·—–-]\s*(working|idle|done|ready|blocked|needs input)\s*$/i,'').trim();
  const redundant=!title||/^(working|idle|done|ready|blocked|needs input)$/i.test(title)
    ||title.toLowerCase()===(pane.workspace||'').toLowerCase()||title.toLowerCase()===agent.toLowerCase();
  return redundant?agent:agent+' · '+title;
}
function makeIcon(name){
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.classList.add('icon');svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');
  const use=document.createElementNS('http://www.w3.org/2000/svg','use');use.setAttribute('href','#icon-'+name);svg.append(use);
  return svg;
}
function contextRow(name,detail,state,selected,activate){
  const row=document.createElement('button');row.className='context-row';row.type='button';
  row.setAttribute('aria-current',String(selected));row.setAttribute('aria-label',name+', '+stateNames[agentState(state)]);
  const dot=document.createElement('span');dot.className='agent-dot '+agentState(state);dot.setAttribute('aria-hidden','true');
  const copy=document.createElement('span');copy.className='context-copy';
  const title=document.createElement('span');title.className='context-name';title.textContent=name;
  const sub=document.createElement('span');sub.className='context-detail';sub.textContent=detail;
  copy.append(title,sub);
  const mark=document.createElement('span');mark.className='context-mark';mark.append(makeIcon(selected?'check':'chevron-right'));
  row.append(dot,copy,mark);row.onclick=activate;
  return row;
}
function renderPicker(){
  const list=$('picker-list'),focused=document.activeElement?.dataset?.pane;list.replaceChildren();
  const spaces=new Map();
  for(const pane of inventory){const id=pane.workspace_id||pane.workspace;if(!spaces.has(id)) spaces.set(id,[]);spaces.get(id).push(pane)}
  $('view-spaces').setAttribute('aria-selected',String(pickerView==='spaces'));
  $('view-agents').setAttribute('aria-selected',String(pickerView==='agents'));
  $('picker-summary').textContent=workspaceFilter?(spaces.get(workspaceFilter)?.[0]?.workspace||'Workspace'):spaces.size+' spaces · '+inventory.length+' panes';
  if(pickerView==='spaces'){
    for(const [id,panes] of spaces){
      list.append(contextRow(panes[0].workspace,panes.length===1?'1 pane':panes.length+' panes',panes[0].workspace_state,
        panes.some(p=>p.id===paneSelect.value),()=>{
          if(panes.length===1) choosePane(panes[0].id);
          else{workspaceFilter=id;pickerView='agents';renderPicker()}
        }));
    }
  }else{
    for(const pane of inventory){
      if(workspaceFilter&&(pane.workspace_id||pane.workspace)!==workspaceFilter) continue;
      const row=contextRow(pane.workspace,paneDetail(pane),pane.state,pane.id===paneSelect.value,()=>choosePane(pane.id));
      row.dataset.pane=pane.id;list.append(row);
      if(focused===pane.id) row.focus({preventScroll:true});
    }
  }
  if(!list.children.length){const empty=document.createElement('p');empty.className='context-detail';empty.textContent='No panes available. Refresh to reconnect.';list.append(empty)}
}
function choosePane(id){if(remoteBusy==='list'){queuedPane=id;return;}if(remoteBusy) return;modifiers.clear();remoteAction({action:'focus',pane:id})}
paneSelect.onclick=()=>{workspaceFilter=null;renderPicker();picker.showModal();if(!inventory.length) remoteAction({action:'list'})};
$('picker-close').onclick=()=>picker.close();
picker.addEventListener('click',e=>{if(e.target===picker) picker.close()});
$('view-spaces').onclick=()=>{pickerView='spaces';workspaceFilter=null;renderPicker()};
$('view-agents').onclick=()=>{pickerView='agents';workspaceFilter=null;renderPicker()};
setInterval(()=>{
  if(!activeDesktop?.herdr||document.hidden||remoteBusy||starting||talking||capturing||!ready) return;
  herdrCommand({action:'list'}).then(result=>{
    if(remoteBusy||starting||talking||capturing) return;
    applyHerdrInventory(result);paintRemote();
  }).catch(()=>{});
},5000);
for(const button of remoteButtons){
  button.addEventListener('contextmenu',e=>e.preventDefault());
  if(button.dataset.paneAction)continue;
  button.onclick=()=>{
    if(button.dataset.mod){
      const mod=button.dataset.mod;modifiers.has(mod)?modifiers.delete(mod):modifiers.add(mod);paintRemote();return;
    }
    if(button.dataset.scroll){remoteAction({action:'scroll',pane:paneSelect.value,direction:button.dataset.scroll});return;}
    const mods=Array.from(modifiers);if(button.dataset.ctrl&&!mods.includes('ctrl')) mods.push('ctrl');
    modifiers.clear();paintRemote();
    remoteAction({action:'key',pane:paneSelect.value,key:button.dataset.key,modifiers:mods});
  };
}
// Desktop controls connect independently of Herdr and the microphone.
diagnostic('page-loaded');
connect().catch(()=>{}); // Initial desktop connection

talk.addEventListener('pointerdown',e=>{ e.preventDefault();
  if(starting||(remoteBusy&&remoteBusy!=='list')) return;
  try{navigator.vibrate&&navigator.vibrate(15)}catch(_){}
  pressed=hf.checked?!talking:true;
  if(hf.checked){ talking?end():begin(); } else begin(); });
['pointerup','pointercancel','pointerleave'].forEach(ev=>
  talk.addEventListener(ev,e=>{ e.preventDefault(); if(!hf.checked){ pressed=false; end(); } }));
// Suppress the browser's delayed long-press menu and its haptic feedback.
talk.addEventListener('touchstart',e=>e.preventDefault(),{passive:false});
talk.addEventListener('contextmenu',e=>e.preventDefault());
hf.onchange=()=>{ try{localStorage.setItem('pm.hf',hf.checked?'1':'0')}catch(e){}
  if(!hf.checked){ pressed=false; end(); } paint(); };
dictation.onchange=()=>{
  if(ready||starting) teardown('Mode changed — ready to reconnect');
  try{localStorage.setItem('pm.dictation',dictation.checked?'1':'0')}catch(e){}
  paint();
};
q.onchange=()=>{ try{localStorage.setItem('pm.q',q.value)}catch(e){}
  if(ready) teardown('Quality changed — hold to reconnect'); };
transport.onchange=()=>{ try{localStorage.setItem('pm.transport',transport.value)}catch(e){}
  if(ready) teardown('Transport changed — hold to reconnect'); };
dis.onclick=()=>{manualDisconnect=true;clearTimeout(reconnectTimer);reconnectTimer=null;teardown('Disconnected');};

// Keep the screen awake only while actually streaming.
async function wake(on){
  try{ if(on&&!lock) lock=await navigator.wakeLock.request('screen');
       else if(!on&&lock){ await lock.release(); lock=null; } }catch(e){}
}
const _b=begin, _e=end;
begin=async()=>{ await _b(); if(talking) wake(true); };
end=()=>{ _e(); wake(false); };
document.addEventListener('visibilitychange',()=>{
  diagnostic('visibility');if(document.hidden){flushDebug();clearTimeout(reconnectTimer);reconnectTimer=null;if(talking)say('Backgrounded — Android may cut the audio','warn');}
  else resumeConnection();
});
globalThis.addEventListener?.('pageshow',resumeConnection);
globalThis.addEventListener?.('online',resumeConnection);
setInterval(()=>resumeConnection(false),5000);
paint();
