#!/usr/bin/env python3
"""phonemic web mic: the phone's browser pushes microphone audio to this laptop.

The phone must be the side that connects out -- Tailscale's Android client
accepts no inbound TCP, and behind CGNAT nothing else reaches the phone either.
So the laptop serves an installable web page; the phone opens it, captures the
microphone, and streams raw PCM over a WebSocket. We pipe that into a
PulseAudio/PipeWire sink that shows up as a normal input device.

Serve plain HTTP behind a TLS-terminating proxy (Cloudflare Tunnel), or supply
PM_CERT/PM_KEY to serve HTTPS directly. getUserMedia requires a secure context
either way.
"""
import asyncio, json, os, pathlib, ssl, subprocess, sys, signal, time

SINK   = os.environ.get("PM_SINK", "phonemic2")
SRC    = os.environ.get("PM_SRC", SINK + "_src")
TOKEN  = os.environ.get("PM_TOKEN", "")
PORT   = int(os.environ.get("PM_PORT", "8444"))
BIND   = os.environ.get("PM_BIND", "127.0.0.1")
CERT   = os.environ.get("PM_CERT", "")
KEY    = os.environ.get("PM_KEY", "")
ASSETS = pathlib.Path(os.environ.get("PM_ASSETS",
                                     pathlib.Path(__file__).resolve().parent.parent / "assets"))
RATE   = int(os.environ.get("PM_RATE", "48000"))
# Requested sink latency. Lower = less delay, more chance of glitching.
LATENCY = int(os.environ.get("PM_LATENCY_MS", "20"))

from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers

# ---------------------------------------------------------------- laptop state

def _run(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return ""

def listeners():
    """Which applications are recording from our virtual microphone.

    This is what makes the phone page useful: it can say whether the laptop is
    actually using the audio, instead of only reporting its own socket.
    """
    idx = None
    for line in _run("pactl", "list", "sources", "short").splitlines():
        f = line.split()
        if len(f) >= 2 and f[1] == SRC:
            idx = f[0]
            break
    if idx is None:
        return None                      # the virtual mic does not exist
    apps, src = [], None
    for line in _run("pactl", "list", "source-outputs").splitlines():
        t = line.strip()
        if t.startswith("Source Output #"):
            src = None
        elif t.startswith("Source:"):
            src = t.split()[1]
        elif t.startswith("application.name") and "=" in t and src == idx:
            name = t.split("=", 1)[1].strip().strip('"')
            if name not in apps:
                apps.append(name)
    return apps

# ---------------------------------------------------------------------- assets

def asset(name, ctype):
    try:
        body = (ASSETS / name).read_bytes()
    except Exception:
        return Response(404, "Not Found", Headers({"Content-Type": "text/plain"}), b"missing\n")
    return Response(200, "OK", Headers({"Content-Type": ctype,
                                        "Content-Length": str(len(body)),
                                        "Cache-Control": "public, max-age=86400"}), body)

def manifest(token):
    q = f"?token={token}" if token else ""
    m = {
        "name": "PhoneMic", "short_name": "PhoneMic",
        "description": "Use this phone as a microphone for your computer.",
        "start_url": "/" + q, "scope": "/", "display": "standalone",
        "orientation": "portrait", "background_color": "#111316",
        "theme_color": "#111316",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/icon-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }
    b = json.dumps(m).encode()
    return Response(200, "OK", Headers({"Content-Type": "application/manifest+json",
                                        "Content-Length": str(len(b))}), b)

SW = b"""
// Minimal service worker: required for installability. Network passthrough --
// the page is tiny and must never be served stale.
self.addEventListener('install',  e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',    e => { return; });
"""

PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>PhoneMic</title>
<link rel=manifest href="/manifest.webmanifest__Q__">
<meta name=theme-color content="#111316">
<meta name=mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black>
<link rel=apple-touch-icon href="/apple-touch-icon.png">
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100dvh;display:flex;flex-direction:column;align-items:center;
justify-content:center;gap:1.3rem;padding:1.5rem;background:#111316;color:#e8eaed;
font:16px/1.5 system-ui,-apple-system,sans-serif;-webkit-tap-highlight-color:transparent;
-webkit-user-select:none;user-select:none;overscroll-behavior:none}
h1{margin:0;font-size:1.1rem;font-weight:600;letter-spacing:.02em}
#talk{width:min(62vw,220px);aspect-ratio:1;border-radius:50%;border:0;
background:#20242a;color:#8b9098;font:600 1.05rem system-ui;display:flex;
align-items:center;justify-content:center;text-align:center;padding:1rem;
box-shadow:0 0 0 0 rgba(46,190,130,.45);transition:background .12s,color .12s,box-shadow .18s;
touch-action:none}
#talk.live{background:#2ebe82;color:#04210f;box-shadow:0 0 0 14px rgba(46,190,130,.12)}
#talk.busy{opacity:.6}
#vis{width:min(88vw,340px);height:66px;background:#171a1e;border-radius:10px}
.key{display:flex;gap:.9rem;font-size:.72rem;opacity:.65;flex-wrap:wrap;justify-content:center}
.key span{display:flex;align-items:center;gap:.32rem}
.key i{width:.6rem;height:.6rem;border-radius:2px;display:inline-block}
.s{font-size:.88rem;text-align:center;max-width:23rem}
#st{opacity:.85}#lap{opacity:.55;font-size:.8rem;min-height:1.2em}
.dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;
background:#555;margin-right:.4rem;vertical-align:middle}
.dot.live{background:#2ebe82}.dot.warn{background:#e0a33a}
label{display:flex;align-items:center;gap:.5rem;font-size:.85rem;opacity:.75}
.row{display:flex;align-items:center;gap:.6rem;font-size:.85rem;opacity:.8}
select{background:#20242a;color:#e8eaed;border:1px solid #2b3037;border-radius:8px;
padding:.45rem .6rem;font:inherit;font-size:.85rem}
input[type=checkbox]{width:1.1rem;height:1.1rem;accent-color:#2ebe82}
#dis{background:none;border:0;color:#8b9098;font-size:.8rem;text-decoration:underline;
padding:.4rem;min-width:0;width:auto;aspect-ratio:auto;border-radius:0;box-shadow:none}
</style></head><body>
<h1>PhoneMic</h1>
<button id=talk>Hold to talk</button>
<canvas id=vis width=680 height=132></canvas>
<div class=key><span><i style="background:#3a4048"></i>not sent</span>
<span><i style="background:#e0a33a"></i>sent</span>
<span><i style="background:#2ebe82"></i>received by laptop</span></div>
<div class=s id=st><span class="dot" id=d></span>Ready</div>
<div class=row>
  <label for=q>Quality</label>
  <select id=q>
    <option value="48000:0">Studio · 48 kHz raw</option>
    <option value="24000:1" selected>Voice · 24 kHz, cleaned up</option>
    <option value="16000:1">Low data · 16 kHz</option>
  </select>
</div>
<label><input type=checkbox id=hf> Hands-free (tap to lock on)</label>
<div class=s id=lap></div>
<button id=dis hidden>disconnect microphone</button>
<script>
const talk=document.getElementById('talk'),st=document.getElementById('st'),
      lap=document.getElementById('lap'),hf=document.getElementById('hf'),
      dis=document.getElementById('dis'),q=document.getElementById('q');
// Quality is applied on the phone before anything is transmitted: a lower rate
// means less data over the air, and "cleaned up" enables the browser's own
// noise suppression / gain control instead of sending raw mic.
try{ const v=localStorage.getItem('pm.q'); if(v) q.value=v; }catch(e){}
const quality=()=>{ const [r,pr]=q.value.split(':'); return {rate:+r, proc:pr==='1'}; };
let ws,ctx,node,stream,lock=null;
let ready=false, talking=false, connecting=false;

// --- waveform -------------------------------------------------------------
// Every frame of captured audio becomes one bar. Colour records its fate:
// grey = captured but not transmitted, amber = sent, green = the laptop
// confirmed it arrived (matched against the byte count the server reports).
const vis=document.getElementById('vis'), g=vis.getContext('2d');
const BARS=110; const hist=[]; let sent=0;
let pend=[], pendN=0, CHUNK=480;
function push(peak,sending){
  hist.push({p:peak, s:sending?1:0, at:sent});
  while(hist.length>BARS) hist.shift();
}
function confirm(rx){
  if(typeof rx!=='number') return;
  for(const h of hist) if(h.s===1 && h.at<=rx) h.s=2;
}
function draw(){
  const W=vis.width, H=vis.height, n=BARS, bw=W/n;
  g.clearRect(0,0,W,H);
  g.fillStyle='#171a1e'; g.fillRect(0,0,W,H);
  g.fillStyle='#22262c'; g.fillRect(0,H/2-1,W,2);
  for(let i=0;i<hist.length;i++){
    const h=hist[i], x=W-(hist.length-i)*bw;
    const amp=Math.max(2, Math.min(1,h.p*1.5)*(H*0.92));
    g.fillStyle = h.s===2?'#2ebe82' : h.s===1?'#e0a33a' : '#3a4048';
    g.fillRect(x+bw*0.18, (H-amp)/2, Math.max(1,bw*0.64), amp);
  }
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);
const say=(t,c)=>{st.innerHTML='<span class="dot '+(c||'')+'"></span>'+t};
if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{});

function paint(){
  talk.className = talking?'live':'';
  talk.textContent = talking ? (hf.checked?'On — tap to stop':'Talking…')
                             : (hf.checked?'Tap to talk':'Hold to talk');
  dis.hidden = !ready;
}
function renderLaptop(m){
  if(m.mic===null){ lap.textContent='Laptop: virtual microphone missing'; return; }
  lap.textContent = (m.apps&&m.apps.length)
    ? 'Laptop is using this mic in: '+m.apps.join(', ')
    : 'Laptop connected. No app has selected "'+m.src+'" yet.';
}
// Connect once and stay connected: re-running getUserMedia on every press
// would clip the first word off everything you say.
async function connect(){
  if(ready||connecting) return ready;
  connecting=true; talk.classList.add('busy'); say('Connecting…');
  const Q=quality();
  try{
    stream=await navigator.mediaDevices.getUserMedia({audio:{
      echoCancellation:Q.proc, noiseSuppression:Q.proc, autoGainControl:Q.proc,
      channelCount:1, sampleRate:Q.rate}});
  }catch(e){ connecting=false; talk.classList.remove('busy');
    say('Microphone blocked ('+e.name+'). Allow it in site settings.','warn'); return false; }
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws'+location.search);
  ws.binaryType='arraybuffer';
  ws.onmessage=e=>{ try{ const m=JSON.parse(e.data); renderLaptop(m); confirm(m.rx); }catch(_){} };
  ws.onclose=()=>{ if(ready) teardown('Disconnected from the laptop','warn'); };
  ws.onerror=()=>say('Connection error','warn');
  try{ await new Promise((r,j)=>{ws.onopen=r; setTimeout(()=>j(new Error('timeout')),10000)}); }
  catch(e){ connecting=false; talk.classList.remove('busy');
    say('Could not reach the laptop','warn'); return false; }
  ctx=new AudioContext({sampleRate:Q.rate, latencyHint:'interactive'});
  // Tell the laptop the real rate the browser gave us -- it may not be the
  // one we asked for, and a mismatch would play back at the wrong speed.
  ws.send(JSON.stringify({rate:ctx.sampleRate, proc:Q.proc}));
  CHUNK=Math.max(128, Math.round(ctx.sampleRate/100));   // ~10 ms per message
  const mod=`class P extends AudioWorkletProcessor{
    process(i){const c=i[0][0]; if(c){const n=new Int16Array(c.length);
      let p=0; for(let k=0;k<c.length;k++){const v=Math.max(-1,Math.min(1,c[k]));
      n[k]=v<0?v*32768:v*32767; if(Math.abs(v)>p)p=Math.abs(v);}
      this.port.postMessage({b:n.buffer,p},[n.buffer]);} return true}}
    registerProcessor('p',P)`;
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([mod],{type:'text/javascript'})));
  node=new AudioWorkletNode(ctx,'p');
  // The mic stays open, but nothing leaves the phone unless you are holding
  // the button. Release really does stop the audio.
  // One WebSocket message per render quantum would be ~375 messages a second;
  // batching to ~10 ms cuts the overhead without adding audible delay.
  node.port.onmessage=e=>{
    const sending = talking && ws && ws.readyState===1;
    push(e.data.p, sending);
    if(!sending){ pend=[]; pendN=0; return; }
    pend.push(new Int16Array(e.data.b)); pendN+=e.data.b.byteLength/2;
    if(pendN>=CHUNK){
      const out=new Int16Array(pendN); let o=0;
      for(const a of pend){ out.set(a,o); o+=a.length; }
      pend=[]; pendN=0; sent+=out.byteLength; ws.send(out.buffer);
    }
  };
  ctx.createMediaStreamSource(stream).connect(node);
  node.connect(ctx.destination);
  try{ lock=await navigator.wakeLock.request('screen'); }catch(e){}
  ready=true; connecting=false; talk.classList.remove('busy');
  say('Ready — hold the button to talk'); paint(); return true;
}
function teardown(msg,c){
  ready=false; talking=false;
  try{ws&&ws.close()}catch(e){} try{ctx&&ctx.close()}catch(e){}
  try{stream&&stream.getTracks().forEach(t=>t.stop())}catch(e){}
  try{lock&&lock.release()}catch(e){} lock=null;
  lap.textContent=''; say(msg||'Disconnected',c); paint();
}
async function begin(){
  if(!ready){ if(!await connect()) return; }
  talking=true; say('Live — streaming to the laptop','live'); paint();
}
function end(){
  if(!talking) return;
  talking=false; say('Ready — hold the button to talk'); paint();
}
talk.addEventListener('pointerdown',e=>{
  e.preventDefault();
  if(hf.checked){ talking?end():begin(); return; }
  begin();
});
['pointerup','pointercancel','pointerleave'].forEach(ev=>
  talk.addEventListener(ev,e=>{ e.preventDefault(); if(!hf.checked) end(); }));
talk.addEventListener('contextmenu',e=>e.preventDefault());
hf.onchange=()=>{ if(!hf.checked) end(); paint(); };
q.onchange=()=>{
  try{ localStorage.setItem('pm.q', q.value); }catch(e){}
  if(ready) teardown('Quality changed — hold the button to reconnect');
};
dis.onclick=()=>teardown('Disconnected');
document.addEventListener('visibilitychange',async()=>{
  if(!document.hidden&&ready&&!lock){ try{lock=await navigator.wakeLock.request('screen')}catch(e){} }
  if(document.hidden&&talking) say('Backgrounded — Android may cut the audio','warn');
});
paint();
</script></body></html>"""

# ---------------------------------------------------------------------- server

def ok_token(path):
    return True if not TOKEN else f"token={TOKEN}" in path

def forbidden():
    return Response(403, "Forbidden", Headers({"Content-Type": "text/plain"}), b"bad token\n")

def process_request(conn, request):
    path = request.path
    base = path.split("?")[0]
    if base == "/sw.js":
        return Response(200, "OK", Headers({"Content-Type": "text/javascript",
                                            "Content-Length": str(len(SW)),
                                            "Service-Worker-Allowed": "/"}), SW)
    if base in ("/icon-192.png", "/icon-512.png", "/icon-maskable-512.png",
                "/apple-touch-icon.png"):
        return asset(base[1:], "image/png")
    if not ok_token(path):
        return forbidden()
    if base == "/manifest.webmanifest":
        return manifest(TOKEN)
    if base == "/ws":
        return None                       # proceed with the websocket handshake
    body = PAGE.replace("__Q__", f"?token={TOKEN}" if TOKEN else "").encode()
    return Response(200, "OK", Headers({"Content-Type": "text/html; charset=utf-8",
                                        "Content-Length": str(len(body)),
                                        "Cache-Control": "no-store"}), body)

async def report(ws, state):
    """Tell the phone what the laptop is doing, and how much audio arrived.

    The byte count is what lets the page colour the waveform by what actually
    landed here, rather than only what the phone believes it sent.
    """
    apps, tick = listeners(), 0
    while True:
        if tick % 8 == 0:                 # pactl is a subprocess; poll it rarely
            apps = listeners()
        tick += 1
        try:
            await ws.send(json.dumps({"src": SRC,
                                      "mic": None if apps is None else True,
                                      "apps": apps or [],
                                      "rx": state["n"]}))
        except Exception:
            return
        await asyncio.sleep(0.25)

def spawn_sink(rate):
    """Feed raw PCM straight into the sink.

    pacat takes a requested latency, which ffmpeg's pulse muxer does not expose
    as directly -- and it keeps ffmpeg out of the hot path entirely.
    """
    return subprocess.Popen(
        ["pacat", "--raw", "--playback", f"--device={SINK}",
         "--format=s16le", f"--rate={rate}", "--channels=1",
         f"--latency-msec={LATENCY}", "--property=application.name=phonemic-web"],
        stdin=subprocess.PIPE)

async def handler(ws):
    peer = ws.remote_address[0] if ws.remote_address else "?"
    # Small frames arrive continuously; Nagle would batch them into extra delay.
    try:
        sock = ws.transport.get_extra_info("socket")
        if sock:
            import socket as _s
            sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
    except Exception:
        pass
    print(f"phone connected: {peer}", flush=True)
    ff = spawn_sink(RATE)
    rate = RATE
    state = {"n": 0}
    task = asyncio.create_task(report(ws, state))
    t0 = time.time()
    try:
        async for msg in ws:
            if isinstance(msg, str):
                # The phone announces its chosen quality before sending audio.
                try:
                    cfg = json.loads(msg)
                except Exception:
                    continue
                r = int(cfg.get("rate", rate))
                if r != rate and 8000 <= r <= 48000:
                    rate = r
                    try: ff.stdin.close()
                    except Exception: pass
                    ff.terminate()
                    ff = spawn_sink(rate)
                    print(f"rate -> {rate} Hz", flush=True)
                continue
            if isinstance(msg, bytes) and ff.stdin:
                # Unflushed, Python holds ~8 KB before writing -- about 170 ms
                # of delay at these rates, for nothing.
                ff.stdin.write(msg); ff.stdin.flush()
                state["n"] += len(msg)
    except Exception as e:
        print("stream ended:", e, flush=True)
    finally:
        task.cancel()
        try: ff.stdin.close()
        except Exception: pass
        ff.terminate()
        print(f"phone disconnected after {time.time()-t0:.0f}s "
              f"({state['n']/2/rate:.1f}s of audio)", flush=True)

async def main():
    ctx = None
    if CERT and KEY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
    async with serve(handler, BIND, PORT, ssl=ctx, process_request=process_request,
                     max_size=None, ping_interval=20):
        print(f"listening on {BIND}:{PORT} ({'https' if ctx else 'http'}) "
              f"sink={SINK} rate={RATE} latency={LATENCY}ms", flush=True)
        await asyncio.get_running_loop().create_future()

if __name__ == "__main__":
    # setsid() forks when the caller already leads a process group, so the
    # shell's $! is the wrapper, not us. Report our real pid ourselves.
    pf = os.environ.get("PM_PIDFILE")
    if pf:
        pathlib.Path(pf).write_text(str(os.getpid()))
    # sys.exit() raises SystemExit, which asyncio swallows, leaving the server
    # alive after SIGTERM. Leave immediately instead.
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
