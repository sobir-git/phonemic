// Run the embedded interaction code with fake browser/audio boundaries.
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const vm = require('node:vm');
const source = execFileSync('python3', ['-c', "import ast,pathlib; tree=ast.parse(pathlib.Path('lib/webmic.py').read_text()); print(next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='PAGE' for t in n.targets)))"], {encoding:'utf8'}).split('<script>')[1].split('</script>')[0].replace("remoteAction({action:'list'});\n\n", '');
function browser(store = {}) {
  const elements = new Map();
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, {
          children:[], replaceChildren(){this.children=[];}, append(child){this.children.push(child);}, showModal(){this.open=true;}, close(){this.open=false;}, focus(){}, textContent: id === "connection-config" ? '{"local":""}' : "", setAttribute(k,v){this[k]=v;}, value: id === 'q' ? '24000:1' : '', checked: false, style: {},
          classList: {add(){}, remove(){}, toggle(){}},
          handlers:{}, addEventListener(type,fn){this.handlers[type]=fn;}, getContext(){return {};},
        });
        return elements.get(id);
      },
      addEventListener(){}, createElement(){return {dataset:{},handlers:{},addEventListener(k,f){this.handlers[k]=f;},setAttribute(){}};}, querySelectorAll(selector){if(selector==='#command-buttons button')return elements.get('command-buttons')?.children||[];if(selector==='[data-command]'||selector==='[data-pane-action]')return [];return [
        {addEventListener(){},dataset:{mod:'ctrl'},setAttribute(k,v){this[k]=v}},
        {addEventListener(){},dataset:{mod:'alt'},setAttribute(k,v){this[k]=v}},
        {addEventListener(){},dataset:{key:'enter'},setAttribute(){}},
      ];},
    },
    navigator: {}, localStorage: {getItem(k){return k in store ? store[k] : null;},setItem(k,v){store[k]=String(v);}}, confirm(){return true;},
    requestAnimationFrame(){}, setTimeout, clearTimeout, setInterval(){},
    performance, console,
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run(`var actualMicOn=micOn; ready=true; ws={readyState:1,close(){}}; pressed=true; micOn=async()=>true;
    var commands=[]; dictationCommand=async action=>{commands.push(action)};`);
  return run;
}
(async () => {
  {
    const run = browser();
    await run(`trackpadPanel.hidden=true; $('open-trackpad').onclick()`);
    assert.equal(run('trackpadPanel.hidden'), false);
    assert.equal(run("$('open-trackpad')['aria-expanded']"), 'true');
    await run("$('open-trackpad').onclick()");
    assert.equal(run('trackpadPanel.hidden'), true);
    run(`trackpadPanel.hidden=false;trackpadPad.setPointerCapture=()=>{};
      var mouseCommands=[];mouseCommand=async c=>mouseCommands.push(c);
      trackpadPad.handlers.pointerdown({preventDefault(){},isPrimary:true,pointerId:1,clientX:20,clientY:20});
      trackpadPad.handlers.pointerup({pointerId:1});`);
    await new Promise(setImmediate);
    assert.equal(run('JSON.stringify(mouseCommands)'), '[{"action":"click","button":"left"}]');
    run(`mouseCommands=[];
      trackpadPad.handlers.pointerdown({preventDefault(){},isPrimary:true,pointerId:2,clientX:20,clientY:20});
      trackpadPad.handlers.pointermove({pointerId:2,clientX:40,clientY:10});
      trackpadPad.handlers.pointerup({pointerId:2});`);
    await new Promise(setImmediate);
    assert.equal(run('JSON.stringify(mouseCommands)'), '[{"action":"move","dx":30,"dy":-15}]');
    run(`mouseCommands=[];
      trackpadPad.handlers.pointerdown({preventDefault(){},isPrimary:true,pointerId:3,clientX:20,clientY:20});
      trackpadPad.handlers.pointercancel();trackpadPad.handlers.pointerup({pointerId:3});`);
    assert.equal(run('mouseCommands.length'), 0);
    run("$('trackpad-right').onclick()");
    await new Promise(setImmediate);
    assert.equal(run('mouseCommands[0].button'), 'right');
  }
  {
    const run = browser();
    run('dictation.checked=true');
    await run('begin()');
    assert.equal(run('talking'), true);
    run('pressed=false; end()');
    await new Promise(setImmediate);
    assert.equal(run('talking'), false);
    assert.equal(run('commands.join(",")'), 'start,stop');
  }
  {
    const run = browser();
    run('dictation.checked=true; var release; dictationCommand=action=>{commands.push(action); return action==="start"?new Promise(r=>release=r):Promise.resolve()};');
    const pending = run('begin()');
    run('pressed=false; end(); release()');
    await pending;
    assert.equal(run('talking'), false);
    assert.equal(run('commands.join(",")'), 'start,abort');
  }
  {
    const run = browser();
    run('dictation.checked=true; var opened=false; micOn=async()=>{opened=true;return true}; dictationCommand=async()=>{throw Error("Dictation is paused or busy")}');
    await run('begin()');
    assert.equal(run('opened'), true);
    assert.equal(run('talking'), false);
    assert.match(run('st.innerHTML'), /paused or busy/);
  }
  {
    const run = browser();
    run('dictation.checked=true; var release; micOn=()=>new Promise(r=>release=r)');
    const pending = run('begin()');
    await new Promise(setImmediate);
    run('pressed=false; end(); release(false)');
    await pending;
    assert.equal(run('commands.join(",")'), 'start,abort');
    assert.equal(run('talking'), false);
  }
  {
    const run = browser();
    await run('begin()');
    run('pressed=false; end()');
    assert.equal(run('commands.length'), 0);
    assert.equal(run('talking'), false);
  }
  {
    const run = browser();
    run(`var reconnects=0; ws.readyState=3;
      connect=async()=>{reconnects++;ws={readyState:1};ready=true;return true;};`);
    await run('begin()');
    assert.equal(run('reconnects'), 1);
    assert.equal(run('talking'), true);
  }
  {
    const run = browser();
    run(`ready=false;
      var sockets=[];
      var location={protocol:'https:',host:'example.test',search:''};
      var URL={createObjectURL(){return 'blob:test'}};
      var Blob=function(){};
      var WebSocket=class {
        constructor(){this.readyState=0;sockets.push(this);Promise.resolve().then(()=>{this.readyState=1;this.onopen()})}
        addEventListener(){} send(){} close(){this.readyState=3}
      };
      var AudioContext=class {
        constructor(){this.sampleRate=24000;this.audioWorklet={addModule:async()=>{}}}
        async suspend(){} close(){}
      };
      var AudioWorkletNode=class {constructor(){this.port={}} connect(){}};`);
    await run('connect()');
    run('ws.readyState=3; pressed=true');
    await run('connect()');
    assert.equal(run('sockets.length'), 2);
    assert.equal(run('pressed'), true);
    run('sockets[0].onclose()');
    assert.equal(run('ready'), true);
    assert.equal(run('ws===sockets[1]'), true);
    run('sockets[1].onclose()');
    assert.equal(run('ready'), false);
    assert.equal(run('ws'), null);
    assert.match(run('st.innerHTML'), /reconnecting/);
    run('dis.onclick()');
  }
  {
    const run = browser();
    run(`dictation.checked=true; var phoneOpened=false, remoteOpened=false, phoneReady,remoteReady;
      micOn=()=>{phoneOpened=true;return new Promise(r=>phoneReady=r)};
      dictationCommand=()=>{remoteOpened=true;return new Promise(r=>remoteReady=r)};`);
    const pending=run('begin()');
    assert.equal(run('phoneOpened && remoteOpened'), true);
    assert.equal(run('talking'), false);
    run('phoneReady(true); remoteReady()');
    await pending;
    assert.equal(run('talking'), true);
  }
  {
    const run = browser();
    run(`var actions=[]; navigator.vibrate=()=>actions.push('vibrate');
      micOn=async()=>{actions.push('mic');return true};
      talk.handlers.pointerdown({preventDefault(){}});`);
    assert.equal(run('actions.join(",")'), 'vibrate,mic');
    await new Promise(setImmediate);
  }
  {
    const run = browser();
    run(`ready=false; dictation.checked=true; var connected, requested=false, packets=[];
      navigator.mediaDevices={getUserMedia(){requested=true;return Promise.resolve({getTracks(){return [{stop(){}}]}})}};
      var URL={createObjectURL(){return 'blob:test'},revokeObjectURL(){}};
      var Blob=function(){};
      var AudioContext=class {
        constructor(){this.sampleRate=24000;this.audioWorklet={addModule:async()=>{}}}
        async suspend(){} async resume(){} close(){}
        createMediaStreamSource(){return {connect(){},disconnect(){}}}
      };
      var AudioWorkletNode=class {constructor(){this.port={}} connect(){}};
      micOn=actualMicOn;
      connect=()=>new Promise(r=>connected=()=>{ready=true;ws={readyState:1,send(b){packets.push(Array.from(new Int16Array(b)))}};r(true)});`);
    const pending=run('begin()');
    assert.equal(run('requested'), true);
    await new Promise(setImmediate);
    assert.equal(run('capturing'), true);
    run('node.port.onmessage({data:{b:new Int16Array([123,456]).buffer,p:0.1}})');
    assert.equal(run('pendN'), 2);
    assert.equal(run('packets.length'), 0);
    run('pressed=false; end()');
    assert.equal(run('capturing'), false);
    assert.equal(run('pendN'), 2);
    run('connected()');
    await pending;
    await new Promise(setImmediate);
    assert.equal(run('packets[0].join(",")'), '123,456');
    assert.equal(run('commands.join(",")'), 'start,stop');
    assert.equal(run('talking'), false);
  }
  {
    const run=browser();
    run(`paneSelect.value='w7:p3'; var remoteCommands=[];
      herdrCommand=async c=>{remoteCommands.push(c);return {pane:c.pane}};
      remoteButtons[0].onclick();remoteButtons[1].onclick();remoteButtons[2].onclick();`);
    await new Promise(setImmediate);
    assert.equal(run('remoteCommands[0].pane'), 'w7:p3');
    assert.equal(run('remoteCommands[0].modifiers.join(",")'), 'ctrl,alt');
    assert.equal(run('modifiers.size'), 0);
    run('talking=true;paintRemote()');
    assert.equal(run('paneSelect.disabled && remoteButtons.every(b=>b.disabled)'), true);
  }
  {
    const run=browser();
    assert.equal(run("paneDetail({workspace:'phonemic',agent:'codex',title:'⠙ phonemic | Working'})"),'codex');
    assert.equal(run("paneDetail({workspace:'phonemic',agent:'codex',title:'Ready'})"),'codex');
    assert.equal(run("paneDetail({workspace:'phonemic',agent:'codex',title:'chatbot | Idle'})"),'codex · chatbot');
    run("syncFocusedPane([{id:'w1:p1',focused:false},{id:'w2:p2',focused:true}])");
    assert.equal(run("paneSelect.value"),'w2:p2');
    run("inventory=[{id:'w2:p2',workspace:'kolokoon_back',agent:'codex',title:'chatbot',state:'idle'}];renderPicked()");
    assert.equal(run("$('picked-detail').textContent"),'codex · chatbot');
    run("applyHerdrInventory({panes:[{id:'w3:p1',workspace:'fire-notes',agent:'codex',title:'1',state:'done',focused:true}]})");
    assert.equal(run("paneSelect.value"),'w3:p1');
    assert.equal(run("$('picked-dot').className"),'agent-dot done');
  }
  {
    const run=browser();
    run('var esc=String.fromCharCode(27)');
    assert.equal(run("terminalRuns(esc+'[31mRed'+esc+'[0m plain')[0].style.fg"),'#fa7777');
    assert.equal(run("terminalRuns(esc+'[38;2;1;2;3mRGB')[0].style.fg"),'rgb(1,2,3)');
    assert.equal(run("terminalRuns(esc+']8;;https://example.test'+String.fromCharCode(7)+'<script>text</script>'+esc+']8;;'+String.fromCharCode(7)).map(r=>r.text).join('')"),'<script>text</script>');
  }
  {
    const run=browser();
    run(`outputPane='w1:p1';var reply,painted=null;
      renderTerminal=text=>painted=text;
      herdrCommand=()=>new Promise(r=>reply=r);`);
    const pending=run('refreshOutput()');
    run("selectOutputPane('w2:p1');reply({pane:'w1:p1',text:'Old pane'})");
    await pending;
    assert.equal(run('painted'),null);
    run("setOutputFollow(false);herdrCommand=()=>{throw Error('Paused reader must not poll')}");
    await run('refreshOutput()');
    assert.equal(run('outputText'),null);
  }
  {
    const run=browser();
    run("$('output-home').hidden=true;outputPane='w1:p1';var reads=0;herdrCommand=async()=>{reads++;return {pane:'w1:p1',text:''}};renderTerminal=()=>{}");
    await run('refreshOutput(true)');assert.equal(run('reads'),0);
    run("$('toggle-output').onclick()");await new Promise(setImmediate);
    assert.equal(run('reads'),1);assert.equal(run("$('toggle-output')['aria-expanded']"),'true');
    run("$('toggle-output').onclick()");await run('refreshOutput()');assert.equal(run('reads'),1);
  }
  {
    const run=browser();
    run("paneSelect.value='w1:p1';paintRemote();var commandSent=[];remoteAction=c=>commandSent.push(c)");
    assert.equal(run("$('command-buttons').children.length"),4);
    run("$('command-buttons').children[0].onclick()");
    assert.equal(run('JSON.stringify(commandSent)'), '[{"action":"command","pane":"w1:p1","text":"/clear"}]');
    run("$('add-command').onclick();$('custom-command').value='my-command';$('save-command').onsubmit({preventDefault(){}})");
    assert.equal(run("$('command-dialog').open"),false);
    assert.equal(run("$('command-buttons').children[4].textContent"),'my-command');
    run("var heldButton=$('command-buttons').children[4];heldButton.handlers.pointerdown({button:0,clientX:0,clientY:0})");
    await new Promise(r=>setTimeout(r,650));
    run('heldButton.handlers.pointerup();heldButton.onclick()');
    assert.equal(run('commandSent.length'),1);
    assert.equal(run('savedCommands.includes("my-command")'),false);
    run("confirm=()=>false;var kept=$('command-buttons').children[0];kept.handlers.contextmenu({preventDefault(){}});kept.onclick()");
    assert.equal(run('savedCommands.includes("/clear")'),true);
    assert.equal(run('commandSent.length'),1);
    run("var moved=$('command-buttons').children[1];moved.handlers.pointerdown({button:0,clientX:0,clientY:0});moved.handlers.pointermove({clientX:20,clientY:0});moved.handlers.pointerup();moved.onclick()");
    assert.equal(run('commandSent.length'),1);
  }
  {
    const run=browser();
    run("outputText='Hello world'+String.fromCharCode(10)+'other line';$('search-output').value='WORLD';$('search-output').oninput()");
    assert.equal(run("$('search-results').textContent"),'1: Hello world');
    assert.equal(run('outputFollow'),false);
    run("paneSelect.value='w2:p3';var inserted;remoteAction=c=>inserted=c;$('composer').value='draft';$('insert-text').onclick()");
    assert.equal(run('JSON.stringify(inserted)'),'{"action":"text","pane":"w2:p3","text":"draft"}');
    assert.equal(run("$('composer').value"),'draft');
    run("completionAlerts=true;inventory=[{id:'p1',state:'working'}];notifyCompletions([{id:'p1',workspace:'Project',agent:'codex',state:'done'}])");
    assert.equal(run("$('notification-status').textContent"),'Project · codex finished');
    run("$('notification-status').textContent='';inventory=[{id:'p1',state:'done'}];notifyCompletions([{id:'p1',state:'done'}])");
    assert.equal(run("$('notification-status').textContent"),'');
    run('var buzz; navigator.vibrate=value=>buzz=value;haptic()');assert.equal(run('buzz'),10);
    run('navigator.vibrate=()=>{throw Error("unsupported")};haptic()');
  }
  {
    const run=browser();
    run("$('command-panel').hidden=$('composer-panel').hidden=true;$('commands-tab').onclick()");
    assert.equal(run("$('command-panel').hidden"),false);
    run("$('composer-tab').onclick()");
    assert.equal(run("$('command-panel').hidden"),true);
    assert.equal(run("$('composer-panel').hidden"),false);
    run("$('composer-tab').onclick()");
    assert.equal(run("$('composer-panel').hidden"),true);
  }
  {
    const run=browser();
    run(`var retryCallback,retryWait;setTimeout=(fn,delay)=>{retryCallback=fn;retryWait=delay;return 42};clearTimeout=()=>{};
      ready=false;scheduleReconnect();`);
    assert.equal(run('retryWait'),1000);
    run('document.hidden=true;retryCallback()');assert.equal(run('reconnectTimer'),null);
    run('document.hidden=false;resumeConnection()');assert.equal(run('retryWait'),0);
    run('manualDisconnect=true;reconnectTimer=null;resumeConnection()');assert.equal(run('reconnectTimer'),null);
    run('manualDisconnect=false;ready=true;lastMessageAt=Date.now()-20000;resumeConnection()');
    assert.equal(run('ready'),false);assert.equal(run('capturing'),false);assert.equal(run('retryWait'),0);
  }
  {
    // settings that should survive a reload
    const run=browser({'pm.output':'1','pm.alerts':'1','pm.draft':'hello draft'});
    assert.equal(run("$('output-home').hidden"),false);
    assert.equal(run("$('toggle-output').textContent"),'Hide output');
    assert.equal(run("$('toggle-output')['aria-expanded']"),'true');
    assert.equal(run('completionAlerts'),true);
    assert.equal(run("$('notifications').textContent"),'Completion alerts: on');
    assert.equal(run("$('notifications')['aria-pressed']"),'true');
    assert.equal(run("$('composer').value"),'hello draft');
  }
  {
    // nothing stored: the restores stay out of the way
    const run=browser();
    assert.equal(run("$('toggle-output').textContent"),'');
    assert.equal(run('completionAlerts'),false);
    assert.equal(run("$('composer').value"),'');
  }
  {
    // toggling writes the choice back
    const run=browser();
    run("$('composer').value='typed';$('composer').handlers.input();");
    run("$('output-home').hidden=true;$('toggle-output').onclick()");
    assert.equal(run("$('output-home').hidden"),false);
    await run("$('notifications').onclick()");
    assert.equal(run('completionAlerts'),true);
    run("$('clear-draft').onclick()");
    assert.equal(run("$('composer').value"),'');
  }
  console.log('Phone interaction checks passed');
})().catch(error => {console.error(error); process.exitCode=1;});
