async function prepareAudio(){
  if(node&&ctx){if(audioCodec==='opus'&&!opusEncoder)await setupOpus();return;}
  if(audioInit) return audioInit;
  if(audioCodec==='opus'&&!await setupOpus()) audioCodec='pcm';
  const audio=ctx=new AudioContext({sampleRate:audioCodec==='opus'?48000:quality().rate,latencyHint:'interactive'});
  const init=(async()=>{
    CHUNK=Math.max(128,Math.round(audio.sampleRate/100));
    const mod=`class P extends AudioWorkletProcessor{
      process(i){const c=i[0][0]; if(c){const n=new Float32Array(c.length);
        let p=0; for(let k=0;k<c.length;k++){const v=Math.max(-1,Math.min(1,c[k]));
        n[k]=v; if(Math.abs(v)>p)p=Math.abs(v);}
        this.port.postMessage({b:n.buffer,p,f:1},[n.buffer]);} return true}}
      registerProcessor('p',P)`;
    const url=URL.createObjectURL(new Blob([mod],{type:'text/javascript'}));
    try{await audio.audioWorklet.addModule(url)}finally{URL.revokeObjectURL(url)}
    if(ctx!==audio) throw new Error('Audio setup cancelled');
    node=new AudioWorkletNode(audio,'p');
    node.port.onmessage=e=>captureChunk(e.data);
    node.connect(audio.destination);
    await audio.suspend();
  })();
  audioInit=init;
  try{await init}finally{if(audioInit===init) audioInit=null}
}
let pend=[],pendN=0,CHUNK=480;

// The selector stores kbps for the wire/profile; WebCodecs requires bits per second.
const opusConfig=()=>({codec:'opus',sampleRate:48000,numberOfChannels:1,bitrate:quality().bitrate*1000,
  opus:{format:'opus',application:'voip',signal:'voice',frameDuration:20000,complexity:5,
    packetlossperc:0,useinbandfec:false,usedtx:false}});
async function setupOpus(){
  if(audioCodec!=='opus'||opusEncoder)return true;
  if(typeof AudioEncoder!=='function'||typeof AudioData!=='function')return false;
  try{
    const config=opusConfig(),support=await AudioEncoder.isConfigSupported(config);
    if(!support?.supported)throw Error('Opus encoding is not supported by this browser');
    opusFailed=false;
    const encoder=new AudioEncoder({output:onOpusOutput,error:error=>{
      opusFailed=true;diagnostic('opus-error',debugError(error));
    }});
    encoder.configure(support.config||config);opusEncoder=encoder;
    return true;
  }catch(error){
    try{opusEncoder&&opusEncoder.close()}catch{} opusEncoder=null;
    diagnostic('opus-unavailable',debugError(error));audioCodec='pcm';audioFraming='pcm';
    if(ws?.readyState===1)ws.send(JSON.stringify({audio_v:2,id:++audioNegotiationId,codec:'pcm',rate:quality().rate,proc:quality().proc}));
    return false;
  }
}
function onOpusOutput(chunk){
  if(!opusStream||!opusReady||(!talking&&!opusEnding)||!ws||ws.readyState!==1)return;
  const body=new Uint8Array(chunk.byteLength);chunk.copyTo(body);
  let wire=body;
  if(audioFraming!=='bare'){
    const packet=new Uint8Array(12+body.byteLength);packet[0]=80;packet[1]=77;packet[2]=2;packet[3]=1;
    new DataView(packet.buffer).setUint32(4,opusStream);new DataView(packet.buffer).setUint32(8,opusSeq++);
    packet.set(body,12);wire=packet;
  }else opusSeq++;
  wireSent+=wire.byteLength;sent+=960*2;ws.send(wire.buffer);
}
function asFloat32(buffer,isFloat=false){
  if(isFloat)return new Float32Array(buffer);
  const source=new Int16Array(buffer),out=new Float32Array(source.length);
  for(let i=0;i<source.length;i++)out[i]=source[i]/32768;return out;
}
function encodeOpusFrame(){
  const frame=new Float32Array(960),take=Math.min(960,opusPendingN);let offset=0;
  while(offset<take){const head=opusPending[0],n=Math.min(head.length,take-offset);
    frame.set(head.subarray(0,n),offset);offset+=n;
    if(n===head.length)opusPending.shift();else opusPending[0]=head.slice(n);
  }
  opusPendingN-=take;
  const audio=new AudioData({format:'f32-planar',sampleRate:48000,numberOfFrames:960,
    numberOfChannels:1,timestamp:opusTimestamp,data:frame.buffer});
  opusTimestamp+=20000;
  try{opusEncoder.encode(audio)}catch(error){opusFailed=true;diagnostic('opus-error',debugError(error));}
  finally{audio.close()}
}
function encodeOpusPending(pad=false){
  if(!opusEncoder||opusFailed||!opusReady)return;
  while(opusPendingN>=960||(pad&&opusPendingN>0)){
    if(!pad&&opusEncoder.encodeQueueSize>=5)break;
    if(!pad&&ws?.bufferedAmount>128*1024){diagnostic('audio-backpressure',{buffered_bytes:ws.bufferedAmount});break;}
    encodeOpusFrame();
  }
}
function waitForEncoderSlot(deadline){
  return new Promise((resolve,reject)=>{
    const check=()=>{
      if(opusFailed)return reject(Error('Opus encoder failed'));
      if(!opusEncoder||Date.now()>=deadline)return reject(Error('Opus encoder did not drain'));
      if(opusEncoder.encodeQueueSize<5)return resolve();
      setTimeout(check,20);
    };
    check();
  });
}
async function drainOpusPending(){
  const deadline=Date.now()+3000;
  while(opusPendingN>0){
    if(!opusEncoder||opusFailed)throw Error('Opus encoder failed');
    if(opusEncoder.encodeQueueSize>=5)await waitForEncoderSlot(deadline);
    if(ws?.bufferedAmount>128*1024)throw Error('Audio output buffer is full');
    encodeOpusFrame();
  }
}
async function startOpus(){
  opusStream++;opusSeq=0;opusTimestamp=0;opusEnding=false;
  opusReady=false;
  if(ws?.readyState!==1)throw Error('Computer disconnected before audio start');
  const streamId=opusStream;
  await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{if(audioReadyWait?.stream===streamId){audioReadyWait=null;reject(Error('Audio start timed out'))}},3000);
    audioReadyWait={stream:streamId,timer,resolve,reject};
    ws.send(JSON.stringify({audio_start:{stream:streamId,rate:48000,framing:audioFraming}}));
  });
  opusReady=true;
}
async function finishOpus(){
  if(!opusEncoder)return true;
  opusEnding=true;
  try{
    await drainOpusPending();
    await Promise.race([opusEncoder.flush(),new Promise((_,reject)=>setTimeout(()=>reject(Error('Opus encoder flush timed out')),3000))]);
    const streamId=opusStream;
    if(ws?.readyState!==1)throw Error('Computer disconnected before audio completion');
    await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{if(audioEndWait?.stream===streamId){audioEndWait=null;reject(Error('Audio completion timed out'))}},3000);
      audioEndWait={stream:streamId,timer,resolve,reject};
      ws.send(JSON.stringify({audio_end:{stream:streamId,lastSeq:opusSeq-1,packetCount:opusSeq}}));
    });
    return true;
  }catch(error){diagnostic('opus-error',debugError(error));return false}
  finally{try{opusEncoder.close()}catch{} opusEncoder=null;opusEnding=false;opusReady=false;}
}

function flushAudio(){
  if(!pendN||!ws||ws.readyState!==1) return;
  const out=new Int16Array(pendN); let offset=0;
  for(const a of pend){out.set(a,offset);offset+=a.length;}
  pend=[]; pendN=0; sent+=out.byteLength;wireSent+=out.byteLength;
  for(let i=0;i<out.length;i+=32000)ws.send(out.slice(i,i+32000).buffer);
}
function captureChunk(data){
  if(!capturing) return;
  push(data.p,talking);
  if(audioCodec==='opus'){
    const samples=asFloat32(data.b,data.f===1);opusPending.push(samples);opusPendingN+=samples.length;
    if(talking)encodeOpusPending();
  }else{
    const pcm=data.f===1?new Int16Array(asFloat32(data.b,true).length):new Int16Array(data.b);
    if(data.f===1){const source=new Float32Array(data.b);for(let i=0;i<source.length;i++){const v=Math.max(-1,Math.min(1,source[i]));pcm[i]=v<0?v*32768:v*32767;}}
    pend.push(pcm);pendN+=pcm.length;
    if(talking&&pendN>=CHUNK)flushAudio();
  }
  const buffered=audioCodec==='opus'?opusPendingN:pendN;
  if(talking&&audioCodec==='opus'&&buffered>48000*2){
    diagnostic('audio-backpressure',{queued_samples:buffered});say('Network is too slow — stopping safely','warn');end();return;
  }
  if(!talking&&buffered>ctx.sampleRate*12){
    diagnostic('startup-buffer-full');micOff();
    if(dictationWait){const pending=dictationWait;dictationWait=null;pending.reject(Error('Laptop dictation took too long to start. Try again.'));}
    say('Laptop dictation took too long to start. Try again.','warn');return;
  }
}
// Request the microphone directly from touch-down, before any network wait.
async function micOn(){
  const Q=quality(), my=++gen;
  const captureRate=audioCodec==='opus'?48000:Q.rate;
  const request=navigator.mediaDevices.getUserMedia({audio:{
    echoCancellation:Q.proc,noiseSuppression:Q.proc,autoGainControl:Q.proc,
    channelCount:1,sampleRate:captureRate}});
  const [phone,graph]=await Promise.allSettled([request,prepareAudio()]);
  if(phone.status==='rejected') throw phone.reason;
  const acquired=phone.value;
  if(my!==gen||graph.status==='rejected'){
    acquired.getTracks().forEach(t=>t.stop());
    if(graph.status==='rejected') throw graph.reason;
    return false;
  }
  stream=acquired;
  src=ctx.createMediaStreamSource(stream); src.connect(node);
  capturing=true;diagnostic('microphone-acquired');
  await ctx.resume();diagnostic('audio-running');
  paint(); say('Recording','live');
  return true;
}
function micOff(keepAudio=false){
  gen++; capturing=false;
  try{ src&&src.disconnect(); }catch(e){} src=null;
  try{ stream&&stream.getTracks().forEach(t=>t.stop()); }catch(e){} stream=null;
  try{ ctx&&ctx.state==='running'&&ctx.suspend(); }catch(e){}
  if(!keepAudio){pend=[]; pendN=0;opusPending=[];opusPendingN=0;}
}
async function begin(){
  if(talking||starting||(remoteBusy&&remoteBusy!=='list')) return;
  starting=true;diagnostic('recording-request');paint();
  let remoteStarted=false;
  try{
    say('Opening microphone…');
    if((!ready||!ws||ws.readyState!==1) && !await connect())
      throw new Error('Could not reach the computer');
    const phone=micOn();
    const remote=(async()=>{
      if(dictation.checked){
        await dictationCommand('start'); remoteStarted=true;
      }
    })();
    const [phoneResult,remoteResult]=await Promise.allSettled([phone,remote]);
    if(remoteResult.status==='rejected') throw remoteResult.reason;
    if(phoneResult.status==='rejected') throw phoneResult.reason;
    if(!phoneResult.value||(!pressed&&!(pendN||opusPendingN))){
      micOff();
      if(remoteStarted) await dictationCommand('abort');
      say('Ready — the mic is off');
      return;
    }
    talking=true;
    if(audioCodec==='opus')await startOpus();
    if(audioCodec==='opus')encodeOpusPending();else flushAudio();diagnostic('recording-streaming');
    say(dictation.checked?'Dictating to laptop':'Live','live');
    if(!pressed){starting=false;end();}
  }catch(e){
    diagnostic('recording-error',debugError(e));talking=false;micOff();
    if(remoteStarted) await dictationCommand('abort').catch(()=>{});
    say(e.message||'Could not start recording','warn');
  }finally{ starting=Boolean(dictationWait); paint(); }
}
