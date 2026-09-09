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
from contextlib import AsyncExitStack

SINK   = os.environ.get("PM_SINK", "phonemic2")
SRC    = os.environ.get("PM_SRC", SINK + "_src")
TOKEN  = os.environ.get("PM_TOKEN", "")
PORT   = int(os.environ.get("PM_PORT", "8444"))
BIND   = os.environ.get("PM_BIND", "127.0.0.1")
CERT   = os.environ.get("PM_CERT", "")
KEY    = os.environ.get("PM_KEY", "")
LOCAL_URL = os.environ.get("PM_LOCAL_URL", "")
LOCAL_BIND = os.environ.get("PM_LOCAL_BIND", "")
LOCAL_PORT = int(os.environ.get("PM_LOCAL_PORT", "8445"))
LOCAL_CERT = os.environ.get("PM_LOCAL_CERT", "")
LOCAL_KEY = os.environ.get("PM_LOCAL_KEY", "")
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
:root{color-scheme:dark;--bg:#07090c;--fg:#f6f8fc;--dim:#b3bbc9;--line:#303a49;
--panel:#10141b;--green:#26d991;--amber:#ffcc4d;--grey:#3a4048}
*{box-sizing:border-box}
.icon{width:20px;height:20px;flex-shrink:0;display:inline-block;vertical-align:middle;
fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}
#remote .direction-pad .icon{width:23px;height:23px;stroke-width:2}
#remote .remote-scroll button,.remote-edit button,#gear,#picker-close{display:flex;align-items:center;justify-content:center;gap:.35rem}
.remote-scroll .icon{width:16px;height:16px}
.context-mark .icon{width:16px;height:16px}
html,body{height:100%}
body{height:auto;min-height:100%}
body{margin:0;background:var(--bg);color:var(--fg);display:flex;flex-direction:column;
font:16px/1.5 system-ui,-apple-system,sans-serif;-webkit-tap-highlight-color:transparent;
-webkit-user-select:none;user-select:none;overscroll-behavior:none;
padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}

/* top bar + settings */
header{display:flex;align-items:center;justify-content:space-between;
padding:.85rem 1.1rem;border-bottom:1px solid var(--line)}
header h1{margin:0;font-size:.95rem;font-weight:600;letter-spacing:.03em}
#gear{background:none;border:0;color:var(--dim);font-size:1.15rem;padding:.3rem .4rem;line-height:1}
#gear.open{color:var(--fg)}
#panel[hidden]{display:none}
#panel{border-bottom:1px solid var(--line);background:var(--panel);
padding:.9rem 1.1rem;display:flex;flex-direction:column;gap:.85rem;font-size:.85rem}
#connection-card{border:1px solid #435166;border-radius:12px;padding:1rem;background:#151e29}
#connection-card[hidden]{display:none}
#connection-card p{color:var(--dim);line-height:1.5;margin:.5rem 0 .8rem}
#connection-card button{background:#26d991;color:#06120c;border:0;border-radius:8px;padding:.7rem 1rem;font:inherit;font-weight:650;width:100%}
#connection-address{display:block;color:var(--dim);overflow-wrap:anywhere;margin-top:.6rem}
.row{display:flex;align-items:center;justify-content:space-between;gap:1rem}
.row label{color:var(--dim)}
select{background:#20242a;color:var(--fg);border:1px solid #2b3037;border-radius:8px;
padding:.4rem .55rem;font:inherit;font-size:.85rem}
input[type=checkbox]{width:1.15rem;height:1.15rem;accent-color:var(--green)}
.key{display:flex;gap:.9rem;flex-wrap:wrap;color:var(--dim);font-size:.72rem}
.key span{display:flex;align-items:center;gap:.3rem}
.key i{width:.6rem;height:.6rem;border-radius:2px}
#dis{background:none;border:1px solid #2b3037;color:var(--dim);border-radius:8px;
padding:.45rem;font:inherit;font-size:.8rem}

/* middle: waveform + one line of status */
main{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
gap:.6rem;padding:1rem;min-height:auto}
#vis{width:100%;max-width:420px;height:40px;background:var(--panel);border-radius:12px}
#st{font-size:.85rem;color:var(--dim);text-align:center;min-height:1.3em}
#lap{font-size:.75rem;color:#6b7079;text-align:center;min-height:1.1em;max-width:24rem}
.dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;
background:#555;margin-right:.4rem;vertical-align:middle}
.dot.live{background:var(--green)}.dot.warn{background:var(--amber)}

#remote{width:100%;max-width:420px;border:1px solid var(--line);border-radius:12px;
background:var(--panel);padding:.8rem;margin-bottom:.5rem}
.remote-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:.5rem}
.remote-head label{font-size:.8rem;color:var(--dim)}
#remote #panes{width:100%;min-height:62px;display:flex;align-items:center;gap:.8rem;
  padding:.7rem .8rem;text-align:left;background:#192841;border-color:#658bd0;border-radius:10px}
.context-copy{flex:1;min-width:0;display:flex;flex-direction:column;gap:.2rem}
.context-name{font-size:.9rem;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.context-detail{font: .72rem/1.4 ui-monospace,SFMono-Regular,monospace;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.context-chevron{font-size:1.1rem;color:#c5d8f8}
.agent-dot{width:9px;height:9px;border-radius:50%;flex:0 0 9px;border:1.5px solid #a1abba;background:transparent}
.agent-dot.idle{border-color:#43e69b}
.agent-dot.working{border-color:#ffcc4d;background:#ffcc4d}
.agent-dot.done{border-color:#57e3da;background:#57e3da}
.agent-dot.blocked{border-color:#ff827a;background:#ff827a}
#pane-picker{position:fixed;inset:auto 0 0;margin:0 auto;width:min(100%,460px);max-width:100%;
  height:min(76dvh,650px);max-height:90dvh;padding:0;border:1px solid #363842;border-bottom:0;
  border-radius:22px 22px 0 0;background:#090e15;color:var(--fg);box-shadow:0 -16px 80px #0008}
#pane-picker[open]{display:flex;flex-direction:column}
#pane-picker::backdrop{background:#05070bb8;backdrop-filter:blur(5px)}
.picker-handle{width:32px;height:4px;background:#434650;border-radius:4px;flex-shrink:0;margin:10px auto 0}
.picker-heading{display:flex;align-items:center;justify-content:space-between;padding:1rem 1.1rem .8rem}
.picker-heading h2{font-size:1.05rem;margin:0;font-weight:600}
.picker-heading p{font-size:.75rem;color:var(--dim);margin:.3rem 0 0}
#picker-close{width:40px;height:40px;border:0;border-radius:50%;background:#1c2532;color:#edf3ff;font-size:1.3rem}
.picker-tabs{display:flex;margin:0 1.1rem .6rem;border-bottom:1px solid #2d3039;gap:1.3rem}
.picker-tabs button{background:none;border:0;border-bottom:2px solid transparent;color:#b3bbc9;
  padding:.7rem 0;font:600 .8rem system-ui;min-height:44px}
.picker-tabs button[aria-selected=true]{border-bottom-color:#8eb9ff;color:#ffffff}
#picker-list{overflow-y:auto;overscroll-behavior:contain;flex:1;padding:.2rem .6rem 1rem}
.context-row{width:100%;display:flex;align-items:center;gap:.85rem;padding:.85rem .7rem;
  min-height:66px;text-align:left;border:1px solid transparent;border-radius:9px;background:none;color:var(--fg)}
.context-row+.context-row{margin-top:2px}
.context-row[aria-current=true]{background:#162a47;border-color:#6799ed}
.context-row:active{background:#303140}
.context-row .context-name{font-size:.85rem;font-weight:500;font-family:ui-monospace,SFMono-Regular,monospace}
.context-row[aria-current=true] .context-name{font-weight:650;color:#ffffff}
.context-mark{color:#a8ccff;font-size:.9rem;width:16px;text-align:center}
#remote button{min-height:40px;background:#1c2532;border:1px solid #435166;
color:var(--fg);border-radius:7px;font:500 .8rem system-ui;touch-action:manipulation}
#remote button:active{background:#39424a}
#remote button:disabled{opacity:.4}
#remote button[aria-pressed=true]{background:var(--green);color:#04210f}
#refresh-panes{padding:0 .65rem;min-height:30px!important}
.remote-tools,.remote-edit{display:flex;gap:.4rem;margin-top:.7rem}
.remote-tools button{flex:1;min-width:0}
.remote-navigation{display:flex;align-items:flex-start;justify-content:space-between;
  gap:1.2rem;padding:.85rem .25rem .25rem}
.direction-pad{display:flex;flex-direction:column;align-items:center;gap:.25rem}
.direction-middle{display:flex;align-items:center;gap:.25rem}
#remote .direction-pad button{width:48px;height:44px;font-size:1.3rem;border-radius:12px;
  box-shadow:0 2px 0 #080b0d;transition:transform .06s,background .06s}
.remote-scroll{display:flex;flex-direction:column;gap:.3rem;width:116px}
#remote .remote-scroll button{min-height:44px;white-space:nowrap}
#remote .remote-scroll [data-scroll=bottom]{min-height:32px;background:transparent;border-color:transparent;color:var(--dim)}
.remote-edit button{flex:1;min-width:0}
#remote .remote-edit [data-key=space]{flex:1.25}
#remote .remote-edit [data-key=enter]{background:#26d991;border-color:#49edac;color:#031b11}
#remote button:active:not(:disabled){transform:translateY(2px);box-shadow:none;background:#405149}
#remote button{touch-action:manipulation;-webkit-touch-callout:none;user-select:none}
#remote-status:empty{display:none}
#remote-status{font-size:.72rem;color:var(--dim);margin-top:.5rem;min-height:1.1em}


#output-viewer{margin-top:.75rem;border:1px solid #303e51;border-radius:10px;overflow:hidden;background:#080c12}
.output-toolbar{display:flex;align-items:center;justify-content:space-between;gap:.5rem;padding:.35rem .6rem;border-bottom:1px solid #293344}
.output-toolbar h2{margin:0;font-size:.75rem;font-weight:600;color:#c4d1e4}
.output-actions{display:flex;align-items:center;gap:.3rem}
#output-viewer button{display:flex;align-items:center;justify-content:center;gap:.3rem;min-height:32px;
  padding:.3rem .45rem;border:1px solid transparent;border-radius:6px;background:transparent;color:#c5d3e8;font:500 .7rem system-ui}
#output-viewer button[aria-pressed=true]{color:#60edb8;background:#0a2b22;border-color:#22694f}
#output-viewer .icon{width:16px;height:16px}
#output-scroll{height:160px;overflow:auto;overscroll-behavior:contain;touch-action:pan-x pan-y;background:#080c12;scrollbar-color:#526580 #080c12}
#terminal-output{margin:0;padding:.6rem .75rem;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;
  color:#e8eff9;white-space:pre;min-height:100%;width:max-content;min-width:100%;user-select:text;-webkit-user-select:text}
#output-error{font-size:.7rem;padding:.35rem .65rem;color:#ffaaa1;border-top:1px solid #293344}
#output-error:empty{display:none}
#output-dialog{position:fixed;inset:0;margin:0;width:100%;max-width:100%;height:100dvh;max-height:100dvh;
  padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
  border:0;background:#080c12;color:var(--fg)}
#output-dialog #output-viewer{height:100%;margin:0;border:0;border-radius:0;display:flex;flex-direction:column}
#output-dialog .output-toolbar{padding:.7rem}
#output-dialog #output-scroll{flex:1;height:auto;min-height:0}
#output-dialog::backdrop{background:#05070be8}

/* bottom: the button */
footer{display:flex;justify-content:center;padding:0 1.2rem clamp(1.2rem,6vh,4rem)}
#talk{width:min(48vw,160px);aspect-ratio:1;border-radius:50%;border:0;
background:#1c2532;color:var(--fg);font:600 1rem system-ui;
display:flex;align-items:center;justify-content:center;text-align:center;padding:1rem;
box-shadow:0 0 0 0 rgba(46,190,130,.4);transition:background .12s,color .12s,box-shadow .2s,transform .1s;
touch-action:none;-webkit-touch-callout:none}
#talk.live{background:var(--green);color:#04210f;box-shadow:0 0 0 16px rgba(46,190,130,.11);transform:scale(1.03)}
#talk.busy{opacity:.55}
footer{position:sticky;bottom:0;background:linear-gradient(transparent,var(--bg) 20%);padding-top:.65rem;padding-bottom:1rem;z-index:2}
#talk{width:120px}
@media(max-height:740px){
  main{justify-content:flex-start;padding:.5rem;gap:.3rem}
  #remote{padding:.6rem;margin-bottom:0}
  #remote button{min-height:36px}
  #vis{height:20px;flex-shrink:0}
  #talk{width:104px}
  footer{padding-bottom:.75rem}
  #lap{min-height:0}
}
</style></head><body>

<svg aria-hidden=true focusable=false style="position:absolute;width:0;height:0;overflow:hidden"><defs><symbol id="icon-up" viewBox="0 0 24 24"><path d="M6 11l6-6 6 6M12 5v14"/></symbol><symbol id="icon-down" viewBox="0 0 24 24"><path d="m6 13 6 6 6-6M12 5v14"/></symbol><symbol id="icon-left" viewBox="0 0 24 24"><path d="m11 6-6 6 6 6M5 12h14"/></symbol><symbol id="icon-right" viewBox="0 0 24 24"><path d="m13 6 6 6-6 6M5 12h14"/></symbol><symbol id="icon-chevron-down" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol><symbol id="icon-chevron-right" viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></symbol><symbol id="icon-check" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></symbol><symbol id="icon-close" viewBox="0 0 24 24"><path d="m6 6 12 12M6 18 18 6"/></symbol><symbol id="icon-backspace" viewBox="0 0 24 24"><path d="M9 5h11v14H9l-7-7 7-7Z"/><path d="m11 9 6 6m-6 0 6-6"/></symbol><symbol id="icon-enter" viewBox="0 0 24 24"><path d="M20 5v7a3 3 0 0 1-3 3H4m5-5-5 5 5 5"/></symbol><symbol id="icon-bottom" viewBox="0 0 24 24"><path d="M12 3v12m-5-5 5 5 5-5M5 21h14"/></symbol><symbol id="icon-settings" viewBox="0 0 24 24"><path d="M4 7h7m6 0h3M4 17h3m6 0h7"/><circle cx="14" cy="7" r="3"/><circle cx="10" cy="17" r="3"/></symbol><symbol id="icon-expand" viewBox="0 0 24 24"><path d="M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5"/></symbol><symbol id="icon-collapse" viewBox="0 0 24 24"><path d="M3 8h5V3m8 0v5h5M8 21v-5H3m18 0h-5v5"/></symbol></defs></svg>
<header>
  <h1>PhoneMic</h1>
  <button id=gear aria-label=Settings><svg class=icon aria-hidden=true focusable=false><use href="#icon-settings"/></svg></button>
</header>

<div id=panel hidden>
  <section id=connection-card hidden aria-label="Connection">
    <strong id=connection-title>Direct over Wi-Fi</strong>
    <p id=connection-help>On the same Wi-Fi as your laptop? Switch to a direct connection for less network delay. No Tailscale needed.</p>
    <button id=switch-connection>Use Wi-Fi connection</button>
    <small id=connection-address></small>
  </section>
  <div class=row><label for=q>Quality</label>
    <select id=q>
      <option value="48000:0">Studio · 48 kHz raw</option>
      <option value="24000:1" selected>Voice · 24 kHz</option>
      <option value="16000:1">Low data · 16 kHz</option>
    </select>
  </div>
  <div class=row><label for=hf>Hands-free (tap to lock on)</label>
    <input type=checkbox id=hf></div>
  <div class=row><label for=dictation>Trigger laptop dictation</label>
    <input type=checkbox id=dictation></div>
  <div class=key>
    <span><i style="background:#3a4048"></i>not sent</span>
    <span><i style="background:#e0a33a"></i>sent</span>
    <span><i style="background:#2ebe82"></i>received</span>
    <span>· 8s window, 1s per line</span>
  </div>
  <button id=dis>Disconnect</button>
</div>

<main>
  <section id=remote aria-label="Herdr remote control">
    <div class=remote-head><label for=panes>Herdr</label><button id=refresh-panes>Refresh</button></div>
    <button id=panes value="" aria-label="Switch workspace or agent" aria-haspopup=dialog aria-controls=pane-picker>
      <span id=picked-dot class=agent-dot aria-hidden=true></span>
      <span class=context-copy><span id=picked-name class=context-name>Choose an agent</span>
      <span id=picked-detail class=context-detail>Spaces &amp; agents on your laptop</span></span>
      <span class=context-chevron aria-hidden=true><svg class=icon aria-hidden=true focusable=false><use href="#icon-chevron-down"/></svg></span>
    </button>
    <div id=output-home>
      <section id=output-viewer aria-label="Agent terminal output">
        <div class=output-toolbar><h2>Live output</h2><div class=output-actions>
          <button id=follow-output aria-pressed=true aria-label="Follow latest output"><svg class=icon aria-hidden=true focusable=false><use href="#icon-bottom"/></svg><span>Follow</span></button>
          <button id=expand-output aria-label="Expand terminal output"><svg class=icon aria-hidden=true focusable=false><use id=expand-output-icon href="#icon-expand"/></svg></button>
        </div></div>
        <div id=output-scroll tabindex=0 aria-label="Terminal screen, scroll to read">
          <pre id=terminal-output>Select a pane to see its output.</pre>
        </div>
        <div id=output-error role=status></div>
      </section>
    </div>
    <div class=remote-tools aria-label="Modifiers and shortcuts">
      <button data-key=esc>Esc</button><button data-key=tab>Tab</button>
      <button data-mod=ctrl aria-pressed=false>Ctrl</button><button data-mod=alt aria-pressed=false>Alt</button>
      <button data-key=c data-ctrl=true>Ctrl+C</button>
    </div>
    <div class=remote-navigation>
      <div class=direction-pad role=group aria-label="Arrow keys">
        <button data-key=up aria-label="Up arrow"><svg class=icon aria-hidden=true focusable=false><use href="#icon-up"/></svg></button>
        <div class=direction-middle>
          <button data-key=left aria-label="Left arrow"><svg class=icon aria-hidden=true focusable=false><use href="#icon-left"/></svg></button>
          <button data-key=down aria-label="Down arrow"><svg class=icon aria-hidden=true focusable=false><use href="#icon-down"/></svg></button>
          <button data-key=right aria-label="Right arrow"><svg class=icon aria-hidden=true focusable=false><use href="#icon-right"/></svg></button>
        </div>
      </div>
      <div class=remote-scroll role=group aria-label="Scroll history">
        <button data-scroll=up><svg class=icon aria-hidden=true focusable=false><use href="#icon-up"/></svg><span>Scroll up</span></button>
        <button data-scroll=down><svg class=icon aria-hidden=true focusable=false><use href="#icon-down"/></svg><span>Scroll down</span></button>
        <button data-scroll=bottom><span>Latest</span><svg class=icon aria-hidden=true focusable=false><use href="#icon-bottom"/></svg></button>
      </div>
    </div>
    <div class=remote-edit aria-label="Editing keys">
      <button data-key=backspace aria-label=Backspace><svg class=icon aria-hidden=true focusable=false><use href="#icon-backspace"/></svg></button>
      <button data-key=space>Space</button>
      <button data-key=enter><span>Enter</span><svg class=icon aria-hidden=true focusable=false><use href="#icon-enter"/></svg></button>
    </div>
    <div id=remote-status role=status>Choose a pane to control.</div>
  </section>
  <canvas id=vis width=840 height=176></canvas>
  <div id=st><span class="dot" id=d></span>Ready</div>
  <div id=lap></div>
</main>

<footer><button id=talk>Touch to talk</button></footer>

<dialog id=output-dialog aria-label="Expanded terminal output"></dialog>
<dialog id=pane-picker aria-labelledby=picker-heading>
  <div class=picker-handle></div>
  <div class=picker-heading><div><h2 id=picker-heading>Herdr</h2><p id=picker-summary>Live Herdr sessions</p></div>
    <button id=picker-close aria-label="Close picker"><svg class=icon aria-hidden=true focusable=false><use href="#icon-close"/></svg></button></div>
  <div class=picker-tabs role=tablist aria-label="Browse Herdr">
    <button id=view-spaces role=tab aria-selected=false>Spaces</button>
    <button id=view-agents role=tab aria-selected=true>Agents</button>
  </div>
  <div id=picker-list role=tabpanel></div>
</dialog>
<script id=connection-config type="application/json">__CONNECTION_CONFIG__</script>
<script>
const $=i=>document.getElementById(i);
const talk=$('talk'),st=$('st'),lap=$('lap'),hf=$('hf'),dis=$('dis'),q=$('q'),
      gear=$('gear'),panel=$('panel'),vis=$('vis'),g=vis.getContext('2d'),dictation=$('dictation');
let ws,ctx,node,src,stream,lock=null;
let ready=false,talking=false,connecting=false,gen=0,pressed=false,starting=false;
let dictationWait=null, audioInit=null, capturing=false;
const paneSelect=$('panes'),remoteStatus=$('remote-status'),refreshPanes=$('refresh-panes');
const remoteButtons=Array.from(document.querySelectorAll('#remote [data-key],#remote [data-mod],#remote [data-scroll]'));
let inventory=[],pickerView='agents',workspaceFilter=null,queuedPane=null;
const picker=$('pane-picker');
const outputViewer=$('output-viewer'),outputScroll=$('output-scroll'),terminalOutput=$('terminal-output');
const outputDialog=$('output-dialog'),followOutput=$('follow-output');
let outputPane='',outputText=null,outputBusy=false,outputFollow=true,outputGeneration=0;
let herdrPending=new Map(),herdrSerial=0,remoteBusy=false,modifiers=new Set(),connectionPromise=null;

try{ const v=localStorage.getItem('pm.q'); if(v) q.value=v; }catch(e){}
try{ hf.checked = localStorage.getItem('pm.hf')==='1'; }catch(e){}
try{ dictation.checked = localStorage.getItem('pm.dictation')==='1'; }catch(e){}
// Carry only preferences across origins; each origin asks for its own mic permission.
try{
  const handoff=new URLSearchParams(location.hash.slice(1));
  if(handoff.has('pmq')){
    if(['48000:0','24000:1','16000:1'].includes(handoff.get('pmq')))q.value=handoff.get('pmq');
    hf.checked=handoff.get('pmhf')==='1';dictation.checked=handoff.get('pmd')==='1';
    localStorage.setItem('pm.q',q.value);localStorage.setItem('pm.hf',hf.checked?'1':'0');
    localStorage.setItem('pm.dictation',dictation.checked?'1':'0');
    history.replaceState(null,'',location.pathname+location.search);
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
  button.onclick=()=>{
    if(talking||starting||capturing){$('connection-help').textContent='Finish recording before switching connections.';return;}
    const next=new URL(target);next.search=location.search;
    next.hash=new URLSearchParams({pmq:q.value,pmhf:hf.checked?'1':'0',pmd:dictation.checked?'1':'0'}).toString();
    location.assign(next.href);
  };
}
setupConnection();
const quality=()=>{ const [r,pr]=q.value.split(':'); return {rate:+r, proc:pr==='1'}; };
const say=(t,c)=>{ st.innerHTML='<span class="dot '+(c||'')+'"></span>'+t; };
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
  if(typeof rx!=='number') return;
  for(const h of hist) if(h.s===1 && h.at<=rx) h.s=2;
}
function draw(){
  if(!talking) push(0,false);        // keep the timeline scrolling when idle
  const W=vis.width,H=vis.height,bw=W/BARS;
  g.fillStyle='#10141b'; g.fillRect(0,0,W,H);
  g.fillStyle='#293445';
  for(let k=PER_SEC;k<BARS;k+=PER_SEC) g.fillRect(W-k*bw,0,1,H);
  g.fillStyle='#435166'; g.fillRect(0,H/2-1,W,2);
  for(let i=0;i<hist.length;i++){
    const h=hist[i],x=W-(hist.length-i)*bw;
    const amp=Math.max(2,Math.min(1,h.p*1.5)*(H*0.9));
    g.fillStyle=h.s===2?'#2ebe82':h.s===1?'#e0a33a':'#3a4048';
    g.fillRect(x+bw*0.15,(H-amp)/2,Math.max(1,bw*0.7),amp);
  }
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);

function paint(){
  talk.className = (talking||capturing)?'live':starting?'busy':'';
  dictation.disabled=q.disabled=hf.disabled=starting||talking;
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
function connect(){
  if(connectionPromise) return connectionPromise;
  connectionPromise=connectSocket().finally(()=>{connectionPromise=null});
  return connectionPromise;
}
async function connectSocket(){
  if(ready&&ws&&ws.readyState===1) return true;
  if(connecting) return false;
  if(ready){ const oldSocket=ws; ws=null; ready=false;
    try{oldSocket&&oldSocket.close()}catch(_){} }
  connecting=true; talk.classList.add('busy'); say('Connecting…');
  const Q=quality();
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws'+location.search);
  const socket=ws;
  ws.binaryType='arraybuffer';
  ws.onmessage=e=>{ if(ws!==socket) return; try{ const m=JSON.parse(e.data);
    if(m.type==='herdr'){
      const pending=herdrPending.get(m.id);
      if(pending){herdrPending.delete(m.id);m.error?pending.reject(new Error(m.error)):pending.resolve(m.result)}
      return;
    }
    if(m.type==='dictation'){
      if(dictationWait){ const pending=dictationWait; dictationWait=null;
        m.error?pending.reject(new Error(m.error)):pending.resolve(m); }
      return;
    }
    renderLaptop(m); confirmRx(m.rx);
  }catch(_){} };
  ws.onclose=()=>{ if(ws===socket) teardown('Connection closed — press to reconnect','warn'); };
  ws.onerror=()=>{ if(ws===socket) say('Connection error','warn'); };
  try{ await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{socket.close();reject(new Error('Connection timed out'))},10000);
    socket.onopen=()=>{clearTimeout(timer);resolve()};
    socket.addEventListener('close',()=>{clearTimeout(timer);reject(new Error('Connection closed'))},{once:true});
  });
  if(ws!==socket||socket.readyState!==1) throw new Error('Connection closed'); }
  catch(e){ connecting=false; talk.classList.remove('busy'); say('Could not reach the computer','warn'); return false; }
  ws.send(JSON.stringify({rate:ctx?ctx.sampleRate:Q.rate,proc:Q.proc}));
  ready=true; connecting=false; talk.classList.remove('busy');
  if(!starting&&!capturing&&!talking) say('Ready — touch to record');
  paint(); return true;
}
async function prepareAudio(){
  if(node&&ctx) return;
  if(audioInit) return audioInit;
  const audio=ctx=new AudioContext({sampleRate:quality().rate,latencyHint:'interactive'});
  const init=(async()=>{
    CHUNK=Math.max(128,Math.round(audio.sampleRate/100));
    const mod=`class P extends AudioWorkletProcessor{
      process(i){const c=i[0][0]; if(c){const n=new Int16Array(c.length);
        let p=0; for(let k=0;k<c.length;k++){const v=Math.max(-1,Math.min(1,c[k]));
        n[k]=v<0?v*32768:v*32767; if(Math.abs(v)>p)p=Math.abs(v);}
        this.port.postMessage({b:n.buffer,p},[n.buffer]);} return true}}
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

function flushAudio(){
  if(!pendN||!ws||ws.readyState!==1) return;
  const out=new Int16Array(pendN); let offset=0;
  for(const a of pend){out.set(a,offset);offset+=a.length;}
  pend=[]; pendN=0; sent+=out.byteLength; ws.send(out.buffer);
}
function captureChunk(data){
  if(!capturing) return;
  push(data.p,talking);
  pend.push(new Int16Array(data.b)); pendN+=data.b.byteLength/2;
  if(!talking&&pendN>ctx.sampleRate*4){
    teardown('Connection too slow — please try again','warn'); return;
  }
  if(talking&&pendN>=CHUNK) flushAudio();
}
// Request the microphone directly from touch-down, before any network wait.
async function micOn(){
  const Q=quality(), my=++gen;
  const request=navigator.mediaDevices.getUserMedia({audio:{
    echoCancellation:Q.proc,noiseSuppression:Q.proc,autoGainControl:Q.proc,
    channelCount:1,sampleRate:Q.rate}});
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
  capturing=true;
  await ctx.resume();
  paint(); say('Recording','live');
  return true;
}
function micOff(keepAudio=false){
  gen++; capturing=false;
  try{ src&&src.disconnect(); }catch(e){} src=null;
  try{ stream&&stream.getTracks().forEach(t=>t.stop()); }catch(e){} stream=null;
  try{ ctx&&ctx.state==='running'&&ctx.suspend(); }catch(e){}
  if(!keepAudio){pend=[]; pendN=0;}
}
async function begin(){
  if(talking||starting||(remoteBusy&&remoteBusy!=='list')) return;
  starting=true; paint();
  let remoteStarted=false;
  try{
    say('Opening microphone…');
    const phone=micOn();
    const remote=(async()=>{
      if((!ready||!ws||ws.readyState!==1) && !await connect())
        throw new Error('Could not reach the computer');
      if(dictation.checked){
        await dictationCommand('start'); remoteStarted=true;
      }
    })();
    const [phoneResult,remoteResult]=await Promise.allSettled([phone,remote]);
    if(remoteResult.status==='rejected') throw remoteResult.reason;
    if(phoneResult.status==='rejected') throw phoneResult.reason;
    if(!phoneResult.value||(!pressed&&!pendN)){
      micOff();
      if(remoteStarted) await dictationCommand('abort');
      say('Ready — the mic is off');
      return;
    }
    talking=true; flushAudio();
    say(dictation.checked?'Dictating to laptop':'Live','live');
    if(!pressed){starting=false;end();}
  }catch(e){
    micOff();
    if(remoteStarted) await dictationCommand('abort').catch(()=>{});
    say(e.message||'Could not start recording','warn');
  }finally{ starting=Boolean(dictationWait); paint(); }
}
function dictationCommand(action){
  return new Promise((resolve,reject)=>{
    if(!ws||ws.readyState!==1){ reject(new Error('Computer disconnected')); return; }
    if(dictationWait){ reject(new Error('Dictation command already pending')); return; }
    const timer=setTimeout(()=>{ dictationWait=null; reject(new Error('Dictation timed out')); ws.close(); },7000);
    dictationWait={resolve:m=>{clearTimeout(timer);resolve(m)},reject:e=>{clearTimeout(timer);reject(e)}};
    ws.send(JSON.stringify({dictation:action,pane:paneSelect.value||undefined}));
  });
}
function end(){
  if(starting){ micOff(true); paint(); return; }
  if(!talking) return;
  flushAudio();
  talking=false; micOff(); paint(); say('Ready — the mic is off');
  if(dictation.checked){
    starting=true; paint(); say('Sending to laptop dictation…');
    dictationCommand('stop').then(()=>say('Sent to laptop for transcription'))
      .catch(e=>say(e.message,'warn')).finally(()=>{starting=false;paint();});
  }
}
function teardown(msg,c){
  pressed=false; ready=false; connecting=false; talking=false; micOff();
  outputGeneration++;if(outputPane)$('output-error').textContent='Disconnected — showing the last screen';
  if(dictationWait){ dictationWait.reject(new Error("Computer disconnected")); dictationWait=null; }
  for(const pending of herdrPending.values()) pending.reject(new Error('Computer disconnected'));
  herdrPending.clear();
  const oldSocket=ws; ws=null;
  try{oldSocket&&oldSocket.close()}catch(e){} try{ctx&&ctx.close()}catch(e){}
  ctx=null; node=null;
  try{lock&&lock.release()}catch(e){} lock=null;
  lap.textContent=''; say(msg||'Disconnected',c); paint();
}

function paintRemote(){
  const busy=starting||talking||capturing||remoteBusy;
  paneSelect.disabled=refreshPanes.disabled=busy;
  for(const button of remoteButtons){
    button.disabled=busy||!paneSelect.value;
    if(button.dataset.mod) button.setAttribute('aria-pressed',String(modifiers.has(button.dataset.mod)));
  }
}
async function herdrCommand(command){
  if(!await connect()) throw new Error('Could not reach the computer');
  return new Promise((resolve,reject)=>{
    const id=++herdrSerial;
    const timer=setTimeout(()=>{herdrPending.delete(id);reject(new Error('Herdr did not respond'))},5000);
    herdrPending.set(id,{resolve:v=>{clearTimeout(timer);resolve(v)},reject:e=>{clearTimeout(timer);reject(e)}});
    ws.send(JSON.stringify({id,herdr:command}));
  });
}
async function remoteAction(command){
  if(remoteBusy||starting||talking||capturing) return;
  remoteBusy=command.action;paintRemote();
  try{
    const result=await herdrCommand(command);
    if(command.action==='list'){
      inventory=result.panes;
      if(!inventory.some(p=>p.id===paneSelect.value)) paneSelect.value=(inventory.find(p=>p.focused)||{}).id||'';
      renderPicked();if(picker.open) renderPicker();
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
    remoteStatus.textContent=error.message;
  }finally{remoteBusy=false;paintRemote();if(command.action==='focus'||command.action==='scroll'||command.action==='key') refreshOutput(true);if(queuedPane){const id=queuedPane;queuedPane=null;choosePane(id)}}
}
refreshPanes.onclick=()=>remoteAction({action:'list'});
const stateNames={idle:'Idle',working:'Working',done:'Done',blocked:'Needs input',unknown:'Terminal'};
function agentState(value){return Object.hasOwn(stateNames,value)?value:'unknown'}
function renderPicked(){
  const pane=inventory.find(p=>p.id===paneSelect.value);
  $('picked-name').textContent=pane?pane.workspace:'Choose an agent';
  $('picked-detail').textContent=pane?(pane.agent||'terminal'):'Spaces & agents on your laptop';
  $('picked-dot').className='agent-dot '+agentState(pane?.state);
  selectOutputPane(paneSelect.value);
}
// Terminal output is untrusted: create text nodes, never HTML or clickable links.
const ansiColors=['#202936','#fa7777','#72dc99','#f2d675','#85b5ff','#d6a0f4','#73dfe4','#dbe5f3',
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
    if(style.reverse){span.style.color=style.bg||'#080c12';span.style.backgroundColor=style.fg||'#e8eff9'}
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
  $('output-error').textContent='';
}
async function refreshOutput(force=false){
  if(outputBusy||!outputPane||!ready||document.hidden||starting||talking||capturing||remoteBusy||(!outputFollow&&!force))return;
  outputBusy=true;
  const pane=outputPane,generation=outputGeneration;
  try{
    const result=await herdrCommand({action:'read',pane});
    if(outputPane!==pane||generation!==outputGeneration||(!outputFollow&&!force))return;
    if(result.pane!==pane||typeof result.text!=='string')throw new Error('Invalid terminal output');
    if(result.text!==outputText){
      outputText=result.text;renderTerminal(result.text||'This pane has no output yet.');
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
  const title=(pane.title||'').replace(/^[\\s\\u2800-\\u28ff✳✻✽✶✢◐◓◑◒⏺●]+/u,'')
    .replace(/\\s*[|·—–-]\\s*(working|idle|done|ready|blocked|needs input)\\s*$/i,'').trim();
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
  row.append(dot,copy,mark);row.onclick=()=>{try{navigator.vibrate&&navigator.vibrate(12)}catch(_){}activate()};
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
  if(document.hidden||remoteBusy||starting||talking||capturing||!ready) return;
  herdrCommand({action:'list'}).then(result=>{
    if(remoteBusy||starting||talking||capturing) return;
    inventory=result.panes;
    if(!inventory.some(p=>p.id===paneSelect.value)) paneSelect.value='';
    renderPicked();paintRemote();if(picker.open) renderPicker();
  }).catch(()=>{});
},5000);
for(const button of remoteButtons){
  button.addEventListener('pointerdown',()=>{
    if(!button.disabled){try{navigator.vibrate&&navigator.vibrate(12)}catch(_){}}
  });
  button.addEventListener('contextmenu',e=>e.preventDefault());
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
// Loading the picker connects the laptop without opening the phone microphone.
remoteAction({action:'list'});

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
dis.onclick=()=>teardown('Disconnected');

// Keep the screen awake only while actually streaming.
async function wake(on){
  try{ if(on&&!lock) lock=await navigator.wakeLock.request('screen');
       else if(!on&&lock){ await lock.release(); lock=null; } }catch(e){}
}
const _b=begin, _e=end;
begin=async()=>{ await _b(); if(talking) wake(true); };
end=()=>{ _e(); wake(false); };
document.addEventListener('visibilitychange',()=>{
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
    config = json.dumps({"local": LOCAL_URL, "public": os.environ.get("PM_PUBLIC_URL", "")}).replace("<", "\\u003c")
    body = PAGE.replace("__Q__", f"?token={TOKEN}" if TOKEN else "").replace("__CONNECTION_CONFIG__", config).encode()
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

def stop_proc(p):
    """Make sure the sink writer really dies.

    terminate() alone can leave it holding a sink-input open, which keeps the
    virtual microphone busy long after the phone has gone.
    """
    if p is None:
        return
    try: p.stdin.close()
    except Exception: pass
    try: p.terminate()
    except Exception: pass
    try:
        p.wait(timeout=2)
    except Exception:
        try: p.kill(); p.wait(timeout=2)
        except Exception: pass

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

class Herdr:
    """Expose only the pane controls used by the phone, over a private socket."""
    async def request(self, method, params):
        path = os.environ.get("PM_HERDR_SOCKET", os.environ.get(
            "HERDR_SOCKET_PATH", str(pathlib.Path.home() / ".config/herdr/herdr.sock")))
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_unix_connection(path, limit=2**20)
            try:
                writer.write((json.dumps({"id": "phonemic", "method": method,
                                          "params": params}) + "\n").encode())
                await writer.drain()
                response = json.loads(await reader.readline())
                if "error" in response:
                    raise RuntimeError(response["error"].get("message", "Herdr command failed"))
                return response["result"]
            finally:
                writer.close()
                await writer.wait_closed()

    async def control(self, message):
        action = message.get("action")
        if action == "list":
            workspaces, panes = await asyncio.gather(
                self.request("workspace.list", {}), self.request("pane.list", {}))
            labels = {w["workspace_id"]: w["label"] for w in workspaces["workspaces"]}
            states = {w["workspace_id"]: w.get("agent_status", "unknown") for w in workspaces["workspaces"]}
            return {"panes": [{"id": p["pane_id"],
                               "workspace": labels.get(p["workspace_id"], p["workspace_id"]),
                               "workspace_id": p["workspace_id"],
                               "workspace_state": states.get(p["workspace_id"], "unknown"),
                               "state": p.get("agent_status", "unknown"),
                               "agent": p.get("agent", "terminal"),
                               "title": p.get("terminal_title_stripped") or p.get("agent") or "Terminal",
                               "focused": p.get("focused", False)} for p in panes["panes"]]}
        pane = message.get("pane")
        if not isinstance(pane, str) or not pane or len(pane) > 128:
            raise RuntimeError("Select a pane first")
        if action == "read":
            result = await self.request("pane.read", {"pane_id": pane, "source": "visible",
                                                      "format": "ansi", "strip_ansi": False, "lines": 160})
            output = result["read"]
            text = output.get("text", "")
            if not isinstance(text, str):
                raise RuntimeError("Herdr returned invalid terminal output")
            return {"pane": pane, "text": text[:120000],
                    "truncated": bool(output.get("truncated")) or len(text) > 120000}
        if action == "focus":
            await self.request("pane.focus", {"pane_id": pane})
        elif action == "scroll":
            direction = message.get("direction")
            if direction not in ("up", "down", "bottom"):
                raise RuntimeError("Unknown scroll direction")
            current = await self.request("pane.get", {"pane_id": pane})
            scroll = current["pane"].get("scroll", {})
            offset = scroll.get("offset_from_bottom", 0)
            step = max(1, scroll.get("viewport_rows", 24) // 2)
            offset = 0 if direction == "bottom" else offset + (step if direction == "up" else -step)
            await self.request("pane.scroll", {"pane_id": pane, "offset_from_bottom":
                               max(0, min(scroll.get("max_offset_from_bottom", 0), offset))})
        elif action == "key":
            key, modifiers = message.get("key"), message.get("modifiers", [])
            if not isinstance(modifiers, list) or len(modifiers) > 2 or any(
                    m not in ("ctrl", "alt") for m in modifiers):
                raise RuntimeError("Unknown modifier")
            if key not in ("esc", "tab", "left", "right", "up", "down", "space", "backspace", "enter"):
                if key != "c" or "ctrl" not in modifiers:
                    raise RuntimeError("Unknown key")
            keys = "+".join([m for m in ("ctrl", "alt") if m in modifiers] + [key])
            await self.request("pane.send_keys", {"pane_id": pane, "keys": [keys]})
        else:
            raise RuntimeError("Unknown Herdr action")
        return {"pane": pane}


class Dictation:
    """One local control connection owns one phone recording."""
    def __init__(self):
        self.reader = self.writer = None

    async def receive(self, expected):
        while True:
            line = await self.reader.readline()
            if not line:
                raise RuntimeError("Laptop dictation disconnected")
            message = json.loads(line)
            if message.get("type") == "error":
                raise RuntimeError(message.get("message", "Laptop dictation failed"))
            if message.get("type") == expected:
                return message

    async def command(self, command):
        self.writer.write((json.dumps(command) + "\n").encode())
        await self.writer.drain()

    async def start(self):
        if self.writer:
            raise RuntimeError("Dictation is already recording")
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/speech-to-text-{os.getuid()}")
        path = os.environ.get("STT_SOCKET_PATH", f"{runtime}/speech-to-text/daemon.sock")
        try:
            async with asyncio.timeout(5):
                self.reader, self.writer = await asyncio.open_unix_connection(path)
                await self.receive("state")
                await self.command({"cmd": "start_recording", "pipewire_node": SRC})
                await self.receive("recording_started")
                # Wait until the capture process produces samples before the phone talks.
                await self.receive("audio_level")
        except Exception:
            await self.close()
            raise

    async def finish(self, abort=False):
        if not self.writer:
            raise RuntimeError("No mobile dictation is recording")
        try:
            async with asyncio.timeout(3):
                if not abort:
                    # Allow the virtual microphone's short playback buffer to drain.
                    await asyncio.sleep(max(0.15, LATENCY / 1000 * 2))
                await self.command({"cmd": "abort_recording" if abort else "stop_recording"})
                await self.receive("recording_stopped")
        finally:
            await self.close()

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except OSError:
                pass
        self.reader = self.writer = None


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
    dictation = Dictation()
    herdr = Herdr()
    state = {"n": 0}
    playback_until = 0.0
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
                if not isinstance(cfg, dict):
                    continue
                if "herdr" in cfg:
                    try:
                        if not TOKEN:
                            raise RuntimeError("Terminal controls require a PhoneMic access token")
                        if not isinstance(cfg["herdr"], dict):
                            raise RuntimeError("Invalid terminal command")
                        result = await herdr.control(cfg["herdr"])
                        await ws.send(json.dumps({"type": "herdr", "id": cfg.get("id"), "result": result}))
                    except Exception as error:
                        message = str(error) if isinstance(error, RuntimeError) else "Herdr is unavailable on the laptop"
                        await ws.send(json.dumps({"type": "herdr", "id": cfg.get("id"), "error": message}))
                    continue
                if "dictation" in cfg:
                    try:
                        action = cfg["dictation"]
                        if action == "start":
                            if cfg.get("pane"):
                                if not TOKEN:
                                    raise RuntimeError("Terminal controls require a PhoneMic access token")
                                await herdr.control({"action": "focus", "pane": cfg["pane"]})
                            await dictation.start()
                        elif action in ("stop", "abort"):
                            if action == "stop":
                                await asyncio.sleep(max(0, playback_until-time.monotonic()))
                            await dictation.finish(abort=action == "abort")
                        else:
                            raise RuntimeError("Unknown dictation action")
                        await ws.send(json.dumps({"type": "dictation", "action": action}))
                    except Exception as error:
                        message = str(error) if isinstance(error, RuntimeError) else "Laptop dictation unavailable; check that its updated daemon is running"
                        await ws.send(json.dumps({"type": "dictation", "error": message}))
                    continue
                r = int(cfg.get("rate", rate))
                if r != rate and 8000 <= r <= 48000:
                    rate = r
                    stop_proc(ff)
                    ff = spawn_sink(rate)
                    print(f"rate -> {rate} Hz", flush=True)
                continue
            if isinstance(msg, bytes) and ff.stdin:
                # Unflushed, Python holds ~8 KB before writing -- about 170 ms
                # of delay at these rates, for nothing.
                playback_until = max(time.monotonic(), playback_until) + len(msg)/2/rate
                ff.stdin.write(msg); ff.stdin.flush()
                state["n"] += len(msg)
    except Exception as e:
        print("stream ended:", e, flush=True)
    finally:
        task.cancel()
        await dictation.close()
        stop_proc(ff)
        print(f"phone disconnected after {time.time()-t0:.0f}s "
              f"({state['n']/2/rate:.1f}s of audio)", flush=True)

async def main():
    ctx = None
    if CERT and KEY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(serve(handler, BIND, PORT, ssl=ctx,
            process_request=process_request, max_size=None, ping_interval=20, ping_timeout=60))
        if LOCAL_BIND:
            if not TOKEN or not LOCAL_URL.startswith("https://"):
                raise RuntimeError("Laptop listener requires HTTPS and an access token")
            local_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            local_ctx.load_cert_chain(LOCAL_CERT, LOCAL_KEY)
            try:
                await stack.enter_async_context(serve(handler, LOCAL_BIND, LOCAL_PORT, ssl=local_ctx,
                    process_request=process_request, max_size=None, ping_interval=20, ping_timeout=60))
                print(f"LAN HTTPS listening on {LOCAL_BIND}:{LOCAL_PORT}", flush=True)
            except OSError:
                # Wi-Fi may be absent at boot. Keep the public endpoint running;
                # the LAN sync timer retries once the interface has an address.
                print("LAN listener unavailable; public endpoint remains active", flush=True)
        print(f"listening on {BIND}:{PORT}", flush=True)
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
