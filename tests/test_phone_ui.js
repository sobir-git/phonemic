// Run the embedded interaction code with fake browser/audio boundaries.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('lib/webmic.py', 'utf8').split('<script>')[1].split('</script>')[0];
function browser() {
  const elements = new Map();
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, {
          value: id === 'q' ? '24000:1' : '', checked: false, style: {},
          classList: {add(){}, remove(){}, toggle(){}},
          addEventListener(){}, getContext(){return {};},
        });
        return elements.get(id);
      },
      addEventListener(){},
    },
    navigator: {}, localStorage: {getItem(){return null;}},
    requestAnimationFrame(){}, setTimeout, clearTimeout,
    performance, console,
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  run(`ready=true; ws={readyState:1,close(){}}; pressed=true; micOn=async()=>true;
    var commands=[]; dictationCommand=async action=>{commands.push(action)};`);
  return run;
}
(async () => {
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
    assert.match(run('st.innerHTML'), /press to reconnect/);
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
  console.log('8 phone interaction checks passed');
})().catch(error => {console.error(error); process.exitCode=1;});
