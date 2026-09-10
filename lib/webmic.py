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
import asyncio, ctypes, ctypes.util, json, os, pathlib, secrets, ssl, subprocess, sys, signal, time
from contextlib import AsyncExitStack
from collections import OrderedDict
import base64, hashlib, re
from http.cookies import SimpleCookie
from urllib.parse import urlsplit
try:
    from .webauth import AuthStore, COOKIE, SESSION_SECONDS, canonical_origin
except ImportError:
    from webauth import AuthStore, COOKIE, SESSION_SECONDS, canonical_origin

SINK   = os.environ.get("PM_SINK", "phonemic2")
SRC    = os.environ.get("PM_SRC", SINK + "_src")
AUTH = AuthStore()
ACTIVE_CONNECTIONS = set()
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

_OPUS = None

def opus_library():
    """Load libopus lazily so PCM-only installations keep working."""
    global _OPUS
    if _OPUS is False:
        return None
    if _OPUS is not None:
        return _OPUS
    try:
        name = ctypes.util.find_library("opus") or "libopus.so.0"
        lib = ctypes.CDLL(name)
        lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int,
                                             ctypes.POINTER(ctypes.c_int)]
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_decoder_destroy.restype = None
        lib.opus_packet_get_nb_samples.argtypes = [ctypes.c_void_p, ctypes.c_int32,
                                                   ctypes.c_int32]
        lib.opus_packet_get_nb_samples.restype = ctypes.c_int
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
                                    ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        lib.opus_decode.restype = ctypes.c_int
        _OPUS = lib
    except Exception:
        _OPUS = False
    return _OPUS if _OPUS is not False else None

class OpusDecoder:
    """Small raw-Opus decoder wrapper for one mono 48 kHz stream."""
    def __init__(self):
        lib = opus_library()
        if lib is None:
            raise RuntimeError("Opus is unavailable on this laptop")
        error = ctypes.c_int(0)
        self.lib = lib
        self.ptr = lib.opus_decoder_create(48000, 1, ctypes.byref(error))
        if not self.ptr or error.value < 0:
            raise RuntimeError("Could not create the Opus decoder")

    def decode(self, packet):
        if not packet or len(packet) > 1275:
            raise RuntimeError("Invalid Opus packet")
        raw = ctypes.create_string_buffer(packet)
        frames = self.lib.opus_packet_get_nb_samples(raw, len(packet), 48000)
        if frames != 960:
            raise RuntimeError("Opus packet is not 20 ms")
        pcm = (ctypes.c_int16 * 960)()
        decoded = self.lib.opus_decode(self.ptr, raw, len(packet), pcm, 960, 0)
        if decoded != 960:
            raise RuntimeError("Opus decoder rejected the packet")
        return bytes(pcm), decoded

    def close(self):
        if self.ptr:
            self.lib.opus_decoder_destroy(self.ptr)
            self.ptr = None

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

def manifest():
    m = {
        "name": "PhoneMic", "short_name": "PhoneMic",
        "description": "Use this phone as a microphone for your computer.",
        "start_url": "/", "scope": "/", "display": "standalone",
        "orientation": "portrait", "background_color": "#000000",
        "theme_color": "#000000",
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
<link rel=manifest href="/manifest.webmanifest">
<meta name=theme-color content="#000000">
<meta name=mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black>
<link rel=apple-touch-icon href="/apple-touch-icon.png">
<style>
:root{color-scheme:dark;
/* OLED: unlit black is the default surface. Panels are drawn with hairlines,
   not fills, and each zone carries its hue in text and borders instead. Solid
   colour is reserved for the moments that are short-lived: a key under a
   thumb, the talk bar while the microphone is open. */
--bg:#000;--well:#000;--surface:#000;--raise:#161D33;--key:#000;
--line:#4E5C92;--edge:#6A79B4;
--fg:#F4F7FF;--dim:#B3C0DA;--mute:#7F8CAC;
--voice:#19F0A6;--voice-ink:#00140D;
--agent:#7AB4FF;--term:#4FE8E8;--pad:#BBA1FF;
--warn:#FFC85C;--alert:#FF8E7F;
--r:14px}
*{box-sizing:border-box}
.icon{width:20px;height:20px;flex-shrink:0;display:inline-block;vertical-align:middle;
fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;pointer-events:none}
#remote .direction-pad .icon{width:24px;height:24px;stroke-width:2.2}
#remote .remote-scroll button,.remote-edit button,#gear,#picker-close{display:flex;align-items:center;justify-content:center;gap:.35rem}
.remote-scroll .icon{width:16px;height:16px}
.context-mark .icon{width:16px;height:16px}
html,body{height:100%}
body{height:auto;min-height:100%}
body{margin:0;background:var(--bg);color:var(--fg);display:flex;flex-direction:column;
font:16px/1.5 system-ui,-apple-system,sans-serif;-webkit-tap-highlight-color:transparent;
-webkit-user-select:none;user-select:none;overscroll-behavior:none;
padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}

/* floating settings key — the top bar is gone, the page starts at the content */
#gear{position:fixed;top:calc(env(safe-area-inset-top) + .45rem);right:calc(env(safe-area-inset-right) + .55rem);
z-index:6;width:40px;height:40px;padding:0;border-radius:12px;line-height:1;
background:var(--bg);border:1px solid var(--line);color:var(--dim)}
#gear.open{border-color:var(--agent);color:var(--agent)}
#gear:active{transform:translateY(2px);background:var(--raise)}

#panel[hidden]{display:none}
#panel{margin:.5rem .55rem 0;border:1px solid var(--line);border-radius:var(--r);
background:var(--surface);border-left:3px solid var(--agent);
padding:2.9rem .9rem .8rem;display:flex;flex-direction:column;gap:.7rem;font-size:.85rem}
#connection-card{border:1px solid var(--line);border-left:3px solid var(--voice);
border-radius:12px;padding:.85rem;background:var(--bg)}
#connection-card[hidden]{display:none}
#connection-card strong{font-size:.92rem;letter-spacing:.01em}
#connection-card p{color:var(--dim);line-height:1.5;margin:.4rem 0 .7rem}
#connection-card button{background:var(--bg);color:var(--voice);border:1px solid var(--voice);border-radius:10px;
padding:.72rem 1rem;font:inherit;font-weight:700;width:100%}
#connection-card button:active{transform:translateY(2px);background:var(--raise)}
#connection-address{display:block;color:var(--dim);overflow-wrap:anywhere;margin-top:.55rem;
font:.75rem/1.4 ui-monospace,SFMono-Regular,monospace}
.row{display:flex;align-items:center;justify-content:space-between;gap:1rem}
.row label{color:var(--dim)}
select{background:var(--key);color:var(--fg);border:1px solid var(--edge);border-radius:9px;
padding:.45rem .6rem;font:inherit;font-size:.85rem}
input[type=checkbox]{width:1.3rem;height:1.3rem;accent-color:var(--voice)}
.key{display:flex;gap:.9rem;flex-wrap:wrap;color:var(--dim);font-size:.72rem}
.key span{display:flex;align-items:center;gap:.35rem}
.key i{width:.65rem;height:.65rem;border-radius:3px}
#dis{background:transparent;border:1px solid var(--alert);color:var(--alert);border-radius:10px;
padding:.55rem;font:600 .8rem system-ui}
#dis:active{background:#2A1116}

/* middle: waveform + one line of status */
main{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;
gap:.5rem;padding:.55rem;min-height:auto}
#vis{width:100%;max-width:480px;height:44px;background:var(--well);border-radius:12px;
border:1px solid var(--line)}
#st{font-size:.85rem;color:var(--fg);text-align:center;min-height:1.3em;font-weight:550}
#lap{font-size:.75rem;color:var(--mute);text-align:center;min-height:1.1em;max-width:24rem}
.dot{display:inline-block;width:.6rem;height:.6rem;border-radius:50%;
background:var(--mute);margin-right:.45rem;vertical-align:middle}
.dot.live{background:var(--voice);box-shadow:0 0 0 4px #19F0A62B}
.dot.warn{background:var(--warn);box-shadow:0 0 0 4px #FFC85C2B}

/* the agent remote — azure is its hue throughout */
#remote{width:100%;max-width:480px;border:1px solid var(--line);border-radius:var(--r);
background:var(--surface);border-top:3px solid var(--agent);padding:.7rem;margin-bottom:.4rem}
.remote-head{display:flex;align-items:center;justify-content:space-between;
  gap:.5rem;padding-right:44px;margin-bottom:.5rem}
.remote-head label{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.8rem;color:var(--fg);font-weight:650;letter-spacing:.02em}
#remote #panes{width:100%;min-height:64px;display:flex;align-items:center;gap:.8rem;
  padding:.7rem .8rem;text-align:left;background:var(--bg);border:1px solid var(--agent);border-radius:12px}
.context-copy{flex:1;min-width:0;display:flex;flex-direction:column;gap:.2rem}
.context-name{font-size:.92rem;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.context-detail{font:.72rem/1.4 ui-monospace,SFMono-Regular,monospace;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.context-chevron{font-size:1.1rem;color:var(--agent)}
.agent-dot{width:10px;height:10px;border-radius:50%;flex:0 0 10px;border:2px solid var(--mute);background:transparent}
.agent-dot.idle{border-color:var(--voice)}
.agent-dot.working{border-color:var(--warn);background:var(--warn);box-shadow:0 0 0 3px #FFC85C2E}
.agent-dot.done{border-color:var(--term);background:var(--term);box-shadow:0 0 0 3px #4FE8E82E}
.agent-dot.blocked{border-color:var(--alert);background:var(--alert);box-shadow:0 0 0 3px #FF8E7F2E}

#pane-picker{position:fixed;inset:auto 0 0;margin:0 auto;width:min(100%,480px);max-width:100%;
  height:min(78dvh,660px);max-height:92dvh;padding:0;border:1px solid var(--line);border-bottom:0;
  border-top:3px solid var(--agent);
  border-radius:20px 20px 0 0;background:var(--bg);color:var(--fg)}
#pane-picker[open]{display:flex;flex-direction:column}
#pane-picker::backdrop{background:#000000C7;backdrop-filter:blur(6px)}
.picker-handle{width:36px;height:4px;background:var(--edge);border-radius:4px;flex-shrink:0;margin:10px auto 0}
.picker-heading{display:flex;align-items:center;justify-content:space-between;padding:.9rem 1rem .7rem}
.picker-heading h2{font-size:1.05rem;margin:0;font-weight:700}
.picker-heading p{font-size:.75rem;color:var(--dim);margin:.3rem 0 0}
#picker-close{width:40px;height:40px;border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--fg);font-size:1.3rem}
.picker-tabs{display:flex;margin:0 1rem .6rem;border-bottom:1px solid var(--line);gap:1.3rem}
.picker-tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--dim);
  padding:.7rem 0;font:650 .82rem system-ui;min-height:44px}
.picker-tabs button[aria-selected=true]{border-bottom-color:var(--agent);color:var(--fg)}
#picker-list{overflow-y:auto;overscroll-behavior:contain;flex:1;padding:.2rem .55rem 1rem}
.context-row{width:100%;display:flex;align-items:center;gap:.85rem;padding:.8rem .7rem;
  min-height:64px;text-align:left;border:1px solid transparent;border-left:3px solid transparent;
  border-radius:10px;background:none;color:var(--fg)}
.context-row+.context-row{margin-top:3px}
.context-row[aria-current=true]{border-left-color:var(--agent)}
.context-row[aria-current=true] .context-name{color:var(--agent)}
.context-row:active{background:var(--raise)}
.context-row .context-name{font-size:.86rem;font-weight:500;font-family:ui-monospace,SFMono-Regular,monospace}
.context-row[aria-current=true] .context-name{font-weight:700}
.context-mark{color:var(--agent);font-size:.9rem;width:16px;text-align:center}

/* keycaps: raised, with a real press */
#remote button{min-height:44px;background:var(--key);border:1px solid var(--edge);
color:var(--fg);border-radius:10px;font:600 .82rem system-ui;touch-action:manipulation}
#remote button:active{background:var(--raise)}
#remote button:disabled{opacity:.38}
#remote button[aria-pressed=true]{background:var(--voice);border-color:var(--voice);color:var(--voice-ink)}
#remote [data-key=c][data-ctrl]{color:var(--alert);border-color:#95504A}
#refresh-panes{padding:0 .7rem;min-height:32px!important}
.remote-tools,.remote-edit{display:flex;gap:.4rem;margin-top:.6rem}
.remote-tools button{flex:1;min-width:0}
.remote-navigation{display:flex;align-items:flex-start;justify-content:space-between;
  gap:1rem;padding:.75rem .1rem .2rem}
.direction-pad{display:flex;flex-direction:column;align-items:center;gap:.3rem}
.direction-middle{display:flex;align-items:center;gap:.3rem}
#remote .direction-pad button{width:52px;height:46px;font-size:1.3rem;border-radius:12px;
  background:var(--key);border-color:var(--edge);transition:transform .06s,background .06s}
.remote-scroll{display:flex;flex-direction:column;gap:.35rem;flex:1;min-width:0;max-width:150px}
#remote .remote-scroll button{min-height:44px;white-space:nowrap}
#remote .remote-scroll [data-scroll=bottom]{min-height:34px;background:transparent;border-color:var(--line);color:var(--dim)}
.remote-edit button{flex:1;min-width:0}
#remote .remote-edit [data-key=space]{flex:1.3}
#remote .remote-edit [data-key=enter]{color:var(--voice);border-color:var(--voice)}
#remote .remote-edit [data-key=enter]:active{background:var(--voice);color:var(--voice-ink)}
#remote button:active:not(:disabled){transform:translateY(2px)}
#remote button{touch-action:manipulation;-webkit-touch-callout:none;user-select:none}
#remote-status:empty{display:none}
#remote-status{font-size:.74rem;color:var(--dim);margin-top:.5rem;min-height:1.1em}

/* terminal output — cyan is its hue */
#output-viewer{margin-top:.7rem;border:1px solid var(--line);border-left:3px solid var(--term);
  border-radius:12px;overflow:hidden;background:var(--well)}
.output-toolbar{display:flex;align-items:center;justify-content:space-between;gap:.5rem;
  padding:.4rem .6rem;border-bottom:1px solid var(--line);background:var(--bg)}
.output-toolbar h2{margin:0;font-size:.76rem;font-weight:650;color:var(--term)}
#remote #open-apps{flex:0 0 40px;width:40px;padding:0;display:flex;align-items:center;justify-content:center}
.output-actions{display:flex;align-items:center;gap:.3rem}
#output-viewer button{display:flex;align-items:center;justify-content:center;gap:.3rem;min-height:32px;
  padding:.3rem .5rem;border:1px solid transparent;border-radius:8px;background:transparent;color:var(--dim);font:600 .72rem system-ui}
#output-viewer button[aria-pressed=true]{color:var(--term);border-color:var(--term)}
#output-viewer .icon{width:16px;height:16px}
[hidden]{display:none!important}
#command-panel{margin:.65rem 0}
.command-row{display:flex;flex-wrap:wrap;gap:.4rem;margin:.5rem 0}
.command-row input,.command-row select{min-width:0;flex:1;background:var(--key);color:var(--fg);border:1px solid var(--edge);border-radius:9px;padding:.6rem}
#workspace-dialog,#command-dialog{width:min(90vw,430px);box-sizing:border-box;background:var(--surface);color:var(--fg);
  border:1px solid var(--line);border-top:3px solid var(--agent);border-radius:16px;padding:1.1rem}
#workspace-dialog::backdrop,#command-dialog::backdrop{background:#000000C7}
#command-dialog button{padding:.7rem 1rem}
#command-buttons{margin:0}
#command-buttons button{overflow-wrap:anywhere;max-width:100%;touch-action:manipulation;-webkit-touch-callout:none}
#add-command{flex:0 0 auto;width:44px;min-width:44px;height:44px;margin:0;padding:0;font-size:1.4rem;line-height:1;
  background:var(--agent);border-color:var(--agent);color:var(--bg);border-radius:10px}
.control-tabs{display:flex;gap:.4rem;margin:.65rem 0}
.control-tabs button{flex:1;min-width:0}
.control-tabs button[aria-expanded=true]{border-color:var(--agent)!important;color:var(--agent)!important}
#composer-panel{margin:.65rem 0}
#composer,#workspace-dialog input,.output-search input{box-sizing:border-box;width:100%;padding:.7rem;
  background:var(--key);color:var(--fg);border:1px solid var(--edge);border-radius:10px;font:inherit;margin:.4rem 0}
#composer{resize:vertical}
#composer:focus,#workspace-dialog input:focus,.output-search input:focus,.command-row input:focus,select:focus{outline:2px solid var(--agent);outline-offset:1px;border-color:var(--agent)}
.output-search{padding:0 .6rem}
.output-search pre{max-height:160px;overflow:auto;white-space:pre-wrap;font-size:.75rem}
.output-search span{font-size:.75rem;color:var(--dim)}
.command-hint{font-size:.75rem;color:var(--dim)}
#output-scroll{height:170px;overflow:auto;overscroll-behavior:contain;touch-action:pan-x pan-y;background:var(--well);scrollbar-color:var(--edge) var(--well)}
#terminal-output{margin:0;padding:.6rem .75rem;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;
  color:#E8EFF9;background:var(--well);white-space:pre;min-height:100%;width:max-content;min-width:100%;user-select:text;-webkit-user-select:text}
#output-error{font-size:.72rem;padding:.35rem .65rem;color:var(--alert);border-top:1px solid var(--line)}
#output-error:empty{display:none}
#output-dialog{position:fixed;inset:0;margin:0;width:100%;max-width:100%;height:100dvh;max-height:100dvh;
  padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
  border:0;background:var(--well);color:var(--fg)}
#output-dialog #output-viewer{height:100%;margin:0;border:0;border-radius:0;display:flex;flex-direction:column}
#output-dialog .output-toolbar{padding:.7rem}
#output-dialog #output-scroll{flex:1;height:auto;min-height:0}
#output-dialog::backdrop{background:#000000EE}

#apps-dialog{box-sizing:border-box;width:min(94vw,480px);max-height:85dvh;background:var(--bg);color:var(--fg);border:1px solid var(--edge);border-radius:18px;padding:16px}
#apps-dialog::backdrop{background:#000c}
.apps-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.apps-head h2{font-size:1.05rem;margin:0}
#apps-previews,#apps-close{padding:10px 14px;background:var(--bg);color:var(--fg);border:1px solid var(--edge);border-radius:10px}
#apps-previews[aria-pressed=false]{color:var(--dim)}
#apps-status{font-size:.78rem;color:var(--dim);margin:12px 0}
#apps-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.app-card{min-width:0;text-align:left;background:var(--bg);color:var(--fg);border:1px solid var(--edge);border-radius:12px;padding:8px;touch-action:manipulation}
.app-card[aria-pressed=true]{border-color:var(--voice)}
.app-card:active{background:var(--raise)}
.app-card:focus-visible{outline:2px solid var(--agent)}
.app-preview{width:100%;aspect-ratio:16/10;object-fit:contain;background:var(--bg);border-radius:6px;display:flex;align-items:center;justify-content:center;color:var(--dim);font-size:.75rem}
.app-name,.app-title{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.app-name{font-weight:650;font-size:.85rem;margin-top:8px}
.app-title{font-size:.72rem;color:var(--dim);margin-top:3px}
/* trackpad — violet is its hue */
#trackpad-panel{margin-top:.7rem;padding:.9rem 0;border:1px solid var(--line);
  border-left:3px solid var(--pad);border-radius:14px;background:var(--well);color:var(--fg)}
#open-trackpad[aria-expanded=true]{border-color:var(--pad)!important;color:var(--pad)!important}
#trackpad-panel button{min-height:44px;padding:.5rem 1rem;border:1px solid var(--edge);
  border-radius:10px;background:var(--key);color:var(--fg);font:600 .82rem system-ui;
  touch-action:manipulation}
#trackpad-panel button:active{transform:translateY(2px);background:var(--raise)}
#trackpad-status{margin:0 .9rem .7rem;color:var(--dim);font-size:.8rem;min-height:2.4em}
#trackpad-pad{height:min(36dvh,280px);margin:0 .9rem;border:1px solid var(--pad);border-radius:16px;
  background:radial-gradient(#BBA1FF40 1px,transparent 1px) 0 0/18px 18px,var(--well);
  display:grid;place-items:center;touch-action:none;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none}
#trackpad-pad span{text-align:center;background:transparent;padding:1rem;color:var(--dim);pointer-events:none}
.trackpad-clicks{display:flex;gap:.6rem;margin:.9rem .9rem 0}
.trackpad-clicks button{flex:1}

/* bottom: the one bold thing — a full-width push-to-talk bar */
footer{display:flex;justify-content:center;padding:0 .55rem clamp(.9rem,4vh,2rem);
position:sticky;bottom:0;background:linear-gradient(transparent,#000 34%);padding-top:.7rem;z-index:5}
#talk{width:100%;max-width:480px;aspect-ratio:auto;height:66px;border-radius:18px;
border:1px solid var(--voice);background:var(--bg);color:var(--voice);
font:700 1.05rem/1.2 system-ui;letter-spacing:.01em;
display:flex;align-items:center;justify-content:center;text-align:center;padding:.8rem;
transition:background .12s,color .12s,box-shadow .18s,transform .1s;
touch-action:none;-webkit-touch-callout:none}
#talk.live{background:var(--voice);color:var(--voice-ink);
box-shadow:0 0 0 5px #19F0A62E;transform:translateY(1px)}
#talk.busy{opacity:.55;border-color:var(--edge);color:var(--dim)}
#talk:active:not(.busy){transform:translateY(3px)}
:focus-visible{outline:2px solid var(--agent);outline-offset:2px}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}

@media(max-height:740px){
  main{justify-content:flex-start;padding:.45rem;gap:.35rem}
  #remote{padding:.6rem;margin-bottom:0}
  #remote button{min-height:40px}
  #vis{height:26px;flex-shrink:0}
  #talk{height:58px}
  footer{padding-bottom:.7rem}
  #lap{min-height:0}
}
</style></head><body>

<svg aria-hidden=true focusable=false style="position:absolute;width:0;height:0;overflow:hidden"><defs><symbol id="icon-apps" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></symbol><symbol id="icon-up" viewBox="0 0 24 24"><path d="M6 11l6-6 6 6M12 5v14"/></symbol><symbol id="icon-down" viewBox="0 0 24 24"><path d="m6 13 6 6 6-6M12 5v14"/></symbol><symbol id="icon-left" viewBox="0 0 24 24"><path d="m11 6-6 6 6 6M5 12h14"/></symbol><symbol id="icon-right" viewBox="0 0 24 24"><path d="m13 6 6 6-6 6M5 12h14"/></symbol><symbol id="icon-chevron-down" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol><symbol id="icon-chevron-right" viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></symbol><symbol id="icon-check" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></symbol><symbol id="icon-close" viewBox="0 0 24 24"><path d="m6 6 12 12M6 18 18 6"/></symbol><symbol id="icon-backspace" viewBox="0 0 24 24"><path d="M9 5h11v14H9l-7-7 7-7Z"/><path d="m11 9 6 6m-6 0 6-6"/></symbol><symbol id="icon-enter" viewBox="0 0 24 24"><path d="M20 5v7a3 3 0 0 1-3 3H4m5-5-5 5 5 5"/></symbol><symbol id="icon-bottom" viewBox="0 0 24 24"><path d="M12 3v12m-5-5 5 5 5-5M5 21h14"/></symbol><symbol id="icon-settings" viewBox="0 0 24 24"><path d="M4 7h7m6 0h3M4 17h3m6 0h7"/><circle cx="14" cy="7" r="3"/><circle cx="10" cy="17" r="3"/></symbol><symbol id="icon-expand" viewBox="0 0 24 24"><path d="M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5"/></symbol><symbol id="icon-collapse" viewBox="0 0 24 24"><path d="M3 8h5V3m8 0v5h5M8 21v-5H3m18 0h-5v5"/></symbol></defs></svg>
<button id=gear aria-label=Settings><svg class=icon aria-hidden=true focusable=false><use href="#icon-settings"/></svg></button>

<div id=panel hidden>
  <section id=connection-card hidden aria-label="Connection">
    <strong id=connection-title>Direct over Wi-Fi</strong>
    <p id=connection-help>Same Wi-Fi, lower latency. No Tailscale.</p>
    <button id=switch-connection>Use Wi-Fi connection</button>
    <small id=connection-address></small>
  </section>
  <div class=row><label for=q>Quality</label>
    <select id=q>
      <option value="48000:1:20" selected>Voice · 48 kHz · 20 kbps</option>
      <option value="48000:1:16">Low data · 48 kHz · 16 kbps</option>
      <option value="48000:0">Studio · 48 kHz raw</option>
      <option value="24000:1">Voice · 24 kHz PCM fallback</option>
      <option value="16000:1">Low data · 16 kHz PCM fallback</option>
    </select>
  </div>
  <div class=row><label for=transport>Transport</label>
    <select id=transport><option value="auto" selected>Auto · Opus</option><option value="pcm">PCM only</option></select>
  </div>
  <div class=row><label for=hf>Hands-free (tap to lock on)</label>
    <input type=checkbox id=hf></div>
  <div class=row><label for=dictation>Trigger laptop dictation</label>
    <input type=checkbox id=dictation></div>
  <div class=key>
    <span><i style="background:#4A5580"></i>not sent</span>
    <span><i style="background:#FFC85C"></i>sent</span>
    <span><i style="background:#19F0A6"></i>received</span>
  </div>
  <button id=dis>Disconnect</button>
</div>

<main>
  <section id=remote aria-label="Desktop remote control">
    <div class=remote-head><label id=active-app-label for=panes>Connecting to desktop…</label><div class=output-actions><button id=toggle-output aria-expanded=false aria-controls=output-home>Show output</button><button id=open-apps aria-label="Applications" title="Applications" aria-haspopup=dialog aria-controls=apps-dialog><svg class=icon aria-hidden=true focusable=false><use href="#icon-apps"/></svg></button><button id=refresh-panes>Refresh</button></div></div>
    <button id=panes value="" aria-label="Switch workspace or agent" aria-haspopup=dialog aria-controls=pane-picker>
      <span id=picked-dot class=agent-dot aria-hidden=true></span>
      <span class=context-copy><span id=picked-name class=context-name>Choose an agent</span>
      <span id=picked-detail class=context-detail>no pane selected</span></span>
      <span class=context-chevron aria-hidden=true><svg class=icon aria-hidden=true focusable=false><use href="#icon-chevron-down"/></svg></span>
    </button>
    <div id=output-home hidden>
      <section id=output-viewer aria-label="Agent terminal output">
        <div class=output-toolbar><h2>Live output</h2><div class=output-actions>
          <button id=follow-output aria-pressed=true aria-label="Follow latest output"><svg class=icon aria-hidden=true focusable=false><use href="#icon-bottom"/></svg><span>Follow</span></button>
          <button id=expand-output aria-label="Expand terminal output"><svg class=icon aria-hidden=true focusable=false><use id=expand-output-icon href="#icon-expand"/></svg></button>
        </div></div>
        <div id=output-scroll tabindex=0 aria-label="Terminal screen, scroll to read">
          <pre id=terminal-output>Select a pane to see its output.</pre>
        </div>
        <div class=output-search><input id=search-output type=search placeholder="Search this screen" aria-label="Search current output"><span id=search-count role=status></span><pre id=search-results hidden></pre></div>
        <div id=output-error role=status></div>
      </section>
    </div>
    <div class=remote-tools aria-label="Modifiers and shortcuts">
      <button data-key=esc>Esc</button><button data-key=tab>Tab</button>
      <button data-mod=ctrl aria-pressed=false>Ctrl</button><button data-mod=alt aria-pressed=false>Alt</button>
      <button data-key=c data-ctrl=true>Ctrl+C</button><button data-key=u data-ctrl=true>Ctrl+U</button>
    </div>
    <div class=control-tabs role=group aria-label="Extra controls">
      <button id=commands-tab aria-expanded=false aria-controls=command-panel>Commands &amp; panes</button>
      <button id=composer-tab aria-expanded=false aria-controls=composer-panel>Type a message</button>
    </div>
    <section id=command-panel hidden aria-labelledby=commands-tab>
      <div class=command-row><div id=command-buttons class=command-row><button id=add-command aria-label="Add command" aria-haspopup=dialog>+</button></div></div>
      <div class=command-row><button id=new-workspace>New space</button><button id=notifications aria-pressed=false>Completion alerts: off</button></div>
      <p id=notification-status class=command-hint role=status></p>
      <div class=command-row><button data-pane-action=split>New pane</button><button data-pane-action=close>Close pane</button></div>
    </section>
    <section id=composer-panel hidden aria-labelledby=composer-tab>
      <label for=composer class=command-hint>Draft</label>
      <textarea id=composer rows=4 maxlength=20000 placeholder="Write or paste a message"></textarea>
      <div class=command-row><button id=insert-text>Type into pane</button><button id=clear-draft>Clear draft</button></div>
    </section>
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
      <button id=open-trackpad aria-expanded=false aria-controls=trackpad-panel>Trackpad</button>
      <button data-key=space>Space</button>
      <button data-key=enter><span>Enter</span><svg class=icon aria-hidden=true focusable=false><use href="#icon-enter"/></svg></button>
    </div>
<section id=trackpad-panel hidden aria-label="Laptop trackpad">
  <p id=trackpad-status role=status>Slide to move. Tap to click.</p>
  <div id=trackpad-pad aria-label="Slide to move the laptop pointer, tap to click"></div>
  <div class=trackpad-clicks><button id=trackpad-left>Left click</button><button id=trackpad-right>Right click</button></div>
</section>
    <div id=remote-status role=status>Choose a pane to control.</div>
  </section>
  <canvas id=vis width=840 height=176></canvas>
  <div id=st><span class="dot" id=d></span>Ready</div>
  <div id=lap></div>
</main>

<footer><button id=talk>Touch to talk</button></footer>


<dialog id=apps-dialog aria-labelledby=apps-heading>
<div class=apps-head><h2 id=apps-heading>Applications</h2><div><button id=apps-previews aria-pressed=true>Previews</button><button id=apps-close aria-label="Close applications">Close</button></div></div>
<p id=apps-status role=status>Loading windows…</p><div id=apps-grid></div>
</dialog>
<dialog id=output-dialog aria-label="Expanded terminal output"></dialog>
<dialog id=workspace-dialog aria-labelledby=workspace-heading>
  <h2 id=workspace-heading>New space</h2>
  <form id=create-workspace>
    <label for=workspace-name>Name</label><input id=workspace-name maxlength=100 required>
    <label for=workspace-directory>Directory, optional</label><input id=workspace-directory maxlength=4096 placeholder="Use selected pane's directory">
    <p id=workspace-error role=status></p>
    <div class=command-row><button type=button id=cancel-workspace>Cancel</button><button id=save-workspace type=submit>Create</button></div>
  </form>
</dialog>
<dialog id=command-dialog aria-labelledby=command-heading>
  <h2 id=command-heading>Add command</h2>
  <form id=save-command>
    <label for=custom-command>Command text</label>
    <div class=command-row><input id=custom-command maxlength=2000 placeholder="Your command" required></div>
    <p id=command-error role=status></p>
    <div class=command-row><button type=button id=cancel-command>Cancel</button><button type=submit>Save</button></div>
  </form>
</dialog>
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
  const matches=plain.split('\\n').map((line,i)=>({line,number:i+1})).filter(row=>row.line.toLowerCase().includes(query));
  results.textContent=matches.map(row=>row.number+': '+row.line).join('\\n');
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
</script></body></html>"""

# ---------------------------------------------------------------------- server

PAIR_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Pair PhoneMic</title>
<link rel=manifest href=/manifest.webmanifest>
<style>body{background:#000;color:#f4f7ff;font:16px/1.5 system-ui;max-width:380px;margin:10vh auto;padding:24px}
input,button{box-sizing:border-box;width:100%;font:inherit;padding:14px;border-radius:10px;margin:8px 0}
input{background:#000;color:inherit;border:1px solid #5a68a0}
button{background:#000;border:1px solid #19f0a6;color:#19f0a6;font-weight:650}
button:active{background:#19f0a6;color:#00140d}
small{color:#b3c0da}code{white-space:nowrap;color:#4fe8e8}#error{color:#ff8e7f}</style></head><body>
<h1>Pair this browser</h1><p>On your computer, run <code>phonemic browser pair</code>, then enter the code below.</p>
<form id=pair-form><label for=code>Pairing code</label><input id=code autocomplete=one-time-code maxlength=32 required autofocus>
<button id=submit>Pair browser</button></form><p id=error role=alert></p>
<small>Only one browser can be paired. Pairing here replaces the previous browser.</small>
<script>
const params=new URLSearchParams(location.hash.slice(1)),ticket=params.get('handoff');
params.delete('handoff');history.replaceState(null,'',location.pathname);
async function exchange(header,value){
 const response=await fetch('/auth/pair',{headers:{[header]:value},cache:'no-store'});
 if(!response.ok)throw Error('Code expired or incorrect. Generate a new code on the computer.');
 history.replaceState(null,'','/'+(params.toString()?'#'+params.toString():''));
 location.reload();
}
document.getElementById('pair-form').onsubmit=async e=>{
 e.preventDefault();document.getElementById('submit').disabled=true;
 try{await exchange('X-PhoneMic-Pairing',document.getElementById('code').value);}
 catch(error){document.getElementById('error').textContent=error.message;}
 finally{document.getElementById('submit').disabled=false;}
};
if(ticket)exchange('X-PhoneMic-Handoff',ticket).catch(()=>{document.getElementById('error').textContent='Connection switch expired. Switch again from the paired page.';});
// Strict cookies may be omitted on the first navigation from another site.
else fetch('/auth/status',{cache:'no-store'}).then(r=>{if(r.ok)location.reload();}).catch(()=>{});
</script></body></html>"""


def origins():
    values = [os.environ.get('PM_PUBLIC_URL', ''), LOCAL_URL]
    values += os.environ.get('PM_ORIGINS', '').split(',')
    values += [f'http://localhost:{PORT}', f'http://127.0.0.1:{PORT}']
    return {origin for value in values if (origin := canonical_origin(value.strip()))}


def request_origin(request):
    host = request.headers.get('Host', '')
    # Never trust forwarded-host/proto headers. Only configured origins are usable.
    candidates = [origin for origin in origins() if urlsplit(origin).netloc == host.lower()]
    supplied = request.headers.get('Origin')
    if supplied is not None:
        normalized = canonical_origin(supplied)
        return normalized if normalized in candidates else None
    return candidates[0] if len(candidates) == 1 else None


def cookie_token(request):
    value = request.headers.get('Cookie', '')
    if len(value) > 4096 or sum(part.strip().split('=', 1)[0] == COOKIE for part in value.split(';')) != 1:
        return None
    try:
        parsed = SimpleCookie(value)
        return parsed[COOKIE].value if COOKIE in parsed else None
    except Exception:
        return None


def secure_response(response):
    if 'Cache-Control' in response.headers:
        del response.headers['Cache-Control']
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Permissions-Policy'] = 'microphone=(self), camera=(), geolocation=()'
    response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    body = response.body.decode(errors='replace')
    scripts = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', body, flags=re.S)
    hashes = ' '.join("'sha256-" + base64.b64encode(hashlib.sha256(code.encode()).digest()).decode() + "'" for code in scripts)
    response.headers['Content-Security-Policy'] = (
        "default-src 'none'; script-src " + ((hashes + ' blob:') if hashes else "'none'") +
        "; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
        "worker-src 'self' blob:; media-src 'self' blob:; manifest-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return response


def reply(status, body='', ctype='text/plain; charset=utf-8', headers=None):
    raw = body.encode()
    return Response(status, {200:'OK',303:'See Other',400:'Bad Request',401:'Unauthorized',
        403:'Forbidden',404:'Not Found',429:'Too Many Requests',503:'Service Unavailable'}[status],
        Headers({'Content-Type':ctype,'Content-Length':str(len(raw)),**(headers or {})}), raw)


def session_cookie(token):
    return f'{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}'


def process_request(conn, request):
    try:
        response = route_request(conn, request)
    except Exception:
        # Corrupt state, duplicate headers, or storage errors never enable access.
        response = reply(503, 'Authentication unavailable')
    return secure_response(response) if response is not None else None


def route_request(conn, request):
    origin = request_origin(request)
    if origin is None:
        return reply(403, 'Unrecognized origin')
    path = urlsplit(request.path)
    base = path.path
    if request.headers.get('Sec-Fetch-Site') == 'cross-site' and (base in ('/ws','/diagnostics') or base.startswith('/auth/')):
        return reply(403, 'Cross-site request denied')
    if path.query:
        return reply(303, headers={'Location':'/'}) if base == '/' else reply(400, 'Query credentials are not supported')
    if base == '/sw.js':
        return Response(200, 'OK', Headers({'Content-Type':'text/javascript','Service-Worker-Allowed':'/'}), SW)
    if base in ('/icon-192.png','/icon-512.png','/icon-maskable-512.png','/apple-touch-icon.png'):
        return asset(base[1:], 'image/png')
    if base == '/manifest.webmanifest':
        return manifest()
    token = cookie_token(request)
    if base == '/auth/pair':
        # Custom headers require a CORS preflight from other sites; we grant no CORS access.
        # This HTTP server accepts GET only, so auth mutations never use query parameters.
        code, ticket = request.headers.get('X-PhoneMic-Pairing'), request.headers.get('X-PhoneMic-Handoff')
        if bool(code) == bool(ticket) or len(code or ticket or '') > 128:
            return reply(400, 'Pairing header required')
        if not PAIR_REQUEST_BUDGET.take():
            return reply(429, 'Wait before retrying pairing')
        session = AUTH.pair(code, origin) if code else AUTH.redeem(ticket, origin)
        if session:
            print('Browser paired' if code else 'Browser connection switched', flush=True)
        return reply(200, headers={'Set-Cookie':session_cookie(session)}) if session else reply(401, 'Pairing failed')
    authenticated = AUTH.valid(token, origin)
    if base == '/':
        if not authenticated:
            return reply(200, PAIR_PAGE, 'text/html; charset=utf-8')
        config = json.dumps({'local':LOCAL_URL,'public':os.environ.get('PM_PUBLIC_URL','')}).replace('<', '\\u003c')
        return reply(200, PAGE.replace('__CONNECTION_CONFIG__',config), 'text/html; charset=utf-8')
    if not authenticated:
        return reply(401, 'Pair this browser on the computer')
    if base == '/diagnostics':
        return receive_diagnostics(request)
    if base == '/auth/status':
        return reply(200)
    if base == '/auth/handoff':
        target = canonical_origin(request.headers.get('X-PhoneMic-Target', ''))
        if target not in origins() or target == origin:
            return reply(400, 'Invalid connection target')
        ticket = AUTH.handoff(token, origin, target)
        return reply(200, json.dumps({'ticket':ticket}), 'application/json') if ticket else reply(401)
    if base == '/ws':
        if not request.headers.get('Origin'):
            return reply(403, 'WebSocket Origin required')
        conn.phonemic_auth = (token, origin)
        return None
    return reply(404, 'Not found')


class Budget:
    def __init__(self, rate, capacity):
        self.rate, self.capacity, self.tokens, self.last = rate, capacity, capacity, time.monotonic()

    def take(self, amount=1):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + max(0, now-self.last)*self.rate)
        self.last = now
        if amount > self.tokens:
            return False
        self.tokens -= amount
        return True


PAIR_REQUEST_BUDGET = Budget(0.5, 10)


DEBUG_BUDGET = Budget(10,40)
DEBUG_EVENTS = set('page-loaded javascript-error unhandled-rejection connection-opening connection-ready connection-closed connection-error connection-teardown microphone-acquired audio-running recording-request recording-streaming recording-progress recording-release recording-error startup-buffer-full audio-backpressure audio-negotiation-failed opus-error opus-unavailable dictation-request dictation-ready dictation-error dictation-timeout visibility desktop-request desktop-ready desktop-error control-request control-error'.split())

def receive_diagnostics(request):
    raw=request.headers.get('X-PhoneMic-Diagnostics','')
    if not raw or len(raw)>12000:return reply(400,'Invalid diagnostics')
    try:events=json.loads(raw)
    except ValueError:return reply(400,'Invalid diagnostics')
    if not isinstance(events,list) or not 1<=len(events)<=8:return reply(400,'Invalid diagnostics')
    if not DEBUG_BUDGET.take(len(events)):return reply(429,'Retry diagnostics later')
    numbers={'seq','time_ms','request_id','socket','code','queued_samples','buffered_bytes','sent_bytes','received_bytes','line','column'}
    flags={'ready','starting','talking','capturing','hidden','online','dictation'}
    enums={'action':{'start','stop','abort','unknown','key','text','command','focus','list','scroll','split','close','workspace','read'},'audio_state':{'none','running','suspended','closed','interrupted'},
           'app_mode':{'herdr','generic','none'},'error':{'Error','TypeError','ReferenceError','SyntaxError','RangeError','DOMException','NotAllowedError','NotFoundError','NotReadableError','NotSupportedError','AbortError','InvalidStateError'}}
    for entry in events:
        if not isinstance(entry,dict) or not isinstance(entry.get('event'),str) or entry['event'] not in DEBUG_EVENTS:continue
        clean={'event':entry['event']}
        for key,value in entry.items():
            if key in numbers and type(value) is int and 0<=value<=10**15:clean[key]=value
            elif key in flags and type(value) is bool:clean[key]=value
            elif key in enums and isinstance(value,str) and value in enums[key]:clean[key]=value
            elif key=='client' and isinstance(value,str) and re.fullmatch(r'[a-z0-9]{1,12}',value):clean[key]=value
        print('phone-debug '+json.dumps(clean,separators=(',',':')),flush=True)
    return reply(200)

async def guard_session(ws):
    while True:
        await asyncio.sleep(1)
        if not AUTH.valid(*ws.phonemic_auth):
            await ws.close(4401, 'Browser access revoked or expired')
            return

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

async def report_desktop(ws):
    while True:
        try:
            context = await desktop_context()
        except Exception:
            context = {}
        try:
            await ws.send(json.dumps({'type':'desktop-context','result':context}))
        except Exception:
            return
        await asyncio.sleep(.8)

async def report_herdr(ws, herdr):
    """Push changed Herdr inventory over the phone socket.

    Herdr's Unix API exposes snapshots, not a long-lived stream. Keep this
    watcher independent from microphone traffic and the browser's timers so
    status changes reach the phone promptly even when its page is throttled.
    """
    previous = None
    while True:
        try:
            inventory = await herdr.control({"action": "list"})
            current = json.dumps(inventory, sort_keys=True, separators=(",", ":"))
            if current != previous:
                await ws.send(json.dumps({"type": "herdr-update", "result": inventory}))
                previous = current
        except asyncio.CancelledError:
            raise
        except Exception:
            # The normal command path reports errors to the user. A transient
            # watcher failure should simply retry on the next pass.
            pass
        await asyncio.sleep(1)

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

def write_audio(process, data):
    # Pipe backpressure must not block WebSocket heartbeats or desktop controls.
    process.stdin.write(data)
    process.stdin.flush()

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
            workspaces, panes, tabs = await asyncio.gather(
                self.request("workspace.list", {}), self.request("pane.list", {}),
                self.request("tab.list", {}))
            labels = {w["workspace_id"]: w["label"] for w in workspaces["workspaces"]}
            states = {w["workspace_id"]: w.get("agent_status", "unknown") for w in workspaces["workspaces"]}
            tab_labels = {t["tab_id"]: t.get("label", "") for t in tabs.get("tabs", [])}
            return {"panes": [{"id": p["pane_id"],
                               "workspace": labels.get(p["workspace_id"], p["workspace_id"]),
                               "workspace_id": p["workspace_id"],
                               "workspace_state": states.get(p["workspace_id"], "unknown"),
                               "state": p.get("agent_status", "unknown"),
                               "agent": p.get("agent", "terminal"),
                               "title": tab_labels.get(p.get("tab_id")) or
                                         p.get("terminal_title_stripped") or p.get("agent") or "Terminal",
                               "focused": p.get("focused", False)} for p in panes["panes"]]}
        if action == "workspace":
            label, cwd = message.get("label"), message.get("cwd", "")
            if not isinstance(label, str) or not label.strip() or len(label) > 100 or any(ord(c) < 32 for c in label):
                raise RuntimeError("Use a space name of up to 100 characters")
            if not isinstance(cwd, str) or len(cwd) > 4096 or any(ord(c) < 32 for c in cwd):
                raise RuntimeError("Invalid directory")
            if cwd and not pathlib.Path(cwd).is_absolute():
                raise RuntimeError("Use an absolute directory path")
            if not cwd and message.get("pane"):
                current = (await self.request("pane.get", {"pane_id": message["pane"]}))["pane"]
                cwd = current.get("foreground_cwd") or current.get("cwd") or ""
            result = await self.request("workspace.create", {"label": label.strip(), "cwd": cwd or None, "focus": True})
            return {"pane": result["root_pane"]["pane_id"]}
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
        if action == "split":
            current = (await self.request("pane.get", {"pane_id": pane}))["pane"]
            result = await self.request("pane.split", {"target_pane_id": pane,
                "workspace_id": current["workspace_id"], "direction": "down",
                "cwd": current.get("foreground_cwd") or current.get("cwd"), "focus": True})
            return {"pane": result["pane"]["pane_id"]}
        elif action == "close":
            await self.request("pane.close", {"pane_id": pane})
        elif action == "text":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 20000 or any(
                    (ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in text):
                raise RuntimeError("Use text of up to 20000 characters without terminal control codes")
            await self.request("pane.send_text", {"pane_id": pane, "text": text})
        elif action == "command":
            text = message.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 2000 or any(ord(c) < 32 or ord(c) == 127 for c in text):
                raise RuntimeError("Use a single-line command of up to 2000 characters")
            await self.request("pane.send_text", {"pane_id": pane, "text": text})
        elif action == "focus":
            await self.request("pane.focus", {"pane_id": pane})
        elif action == "scroll":
            direction = message.get("direction")
            if direction not in ("up", "down", "bottom"):
                raise RuntimeError("Unknown scroll direction")
            current = await self.request("pane.get", {"pane_id": pane})
            scroll = current["pane"].get("scroll", {})
            if current["pane"].get("agent") == "claude" and not scroll.get("max_offset_from_bottom", 0):
                layout = (await self.request("pane.layout", {"pane_id": pane}))["layout"]
                rect = next(p["rect"] for p in layout["panes"] if p["pane_id"] == pane)
                x, y = max(1, rect["width"] // 2), max(1, rect["height"] // 2)
                # Herdr 0.9 sends text as raw PTY bytes but rejects wheel/PageUp keys.
                # SGR mouse reports go straight to Claude, independent of desktop focus.
                wheel = f"\x1b[<{64 if direction == 'up' else 65};{x};{y}M"
                if direction == "bottom":
                    async def screen():
                        result = await self.request("pane.read", {"pane_id": pane,
                            "source": "visible", "format": "text", "lines": 160})
                        return result["read"]["text"]
                    previous = await screen()
                    for _ in range(10):
                        await self.request("pane.send_text", {"pane_id": pane, "text": wheel * 40})
                        await asyncio.sleep(0.1)
                        current_screen = await screen()
                        if current_screen == previous:
                            break
                        previous = current_screen
                    else:
                        raise RuntimeError("Moved toward latest output. Tap Latest again to continue.")
                else:
                    await self.request("pane.send_text", {"pane_id": pane, "text": wheel * 3})
                return {"pane": pane}
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
                if key not in ("c", "u") or "ctrl" not in modifiers:
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


CONTEXT_CACHE = None
CONTEXT_UPDATED = 0.0
CONTEXT_LOCK = asyncio.Lock()

async def desktop_context(fresh=False):
    global CONTEXT_CACHE, CONTEXT_UPDATED
    async with CONTEXT_LOCK:
        if not fresh and CONTEXT_CACHE is not None and time.monotonic()-CONTEXT_UPDATED < .4:
            return CONTEXT_CACHE
        CONTEXT_CACHE = await desktop_snapshot(context=True)
        CONTEXT_UPDATED = time.monotonic()
        return CONTEXT_CACHE

async def require_desktop(window, herdr=None):
    current = await desktop_context(fresh=True)
    if not window or current.get('id') != window or (herdr is not None and current.get('herdr') != herdr):
        raise RuntimeError('Focused app changed. Check the current app and try again.')
    return current

async def route_desktop_input(message, herdr):
    command=message.get('command')
    if not isinstance(command,dict) or command.get('action') not in ('key','scroll','text','command') or type(message.get('herdr')) is not bool:
        raise RuntimeError('Invalid input command')
    current=await require_desktop(message.get('window'),message['herdr'])
    if current['herdr']:
        return await herdr.control(command)
    return await generic_input(command,current['id'])

async def generic_input(command, window):
    if not isinstance(command,dict):
        raise RuntimeError('Invalid input')
    action = command.get('action')
    data = None
    if action == 'key':
        keys = {'esc':'Escape','tab':'Tab','left':'Left','right':'Right','up':'Up','down':'Down','space':'space','backspace':'BackSpace','enter':'Return','c':'c','u':'u'}
        key, mods = command.get('key'), command.get('modifiers',[])
        if key not in keys or not isinstance(mods,list) or len(mods)>2 or any(m not in ('ctrl','alt') for m in mods):
            raise RuntimeError('Invalid key')
        args = ['key','--clearmodifiers','+'.join(mods+[keys[key]])]
    elif action in ('text','command'):
        text = command.get('text')
        if not isinstance(text,str) or not text.strip() or len(text)>20000 or any((ord(c)<32 and c not in '\n\t') or ord(c)==127 for c in text):
            raise RuntimeError('Use text without control codes, up to 20000 characters')
        args = ['type','--clearmodifiers','--delay','0','--file','-'];data=text.encode()
    elif action == 'scroll':
        direction=command.get('direction')
        if direction == 'bottom':
            args=['key','--clearmodifiers','ctrl+End']
        elif direction in ('up','down'):
            # Scroll inside the active window, rather than whichever app is under the pointer.
            current=await require_desktop(window, False)
            args=['mousemove','--window',window,str(current['width']//2),str(current['height']//2),'click','--repeat','3','--delay','30','4' if direction=='up' else '5']
        else:
            raise RuntimeError('Invalid scroll direction')
    else:
        raise RuntimeError('Unsupported desktop input')
    await require_desktop(window, False)
    proc = await asyncio.create_subprocess_exec('xdotool',*args,stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.communicate(data),timeout=5)
    finally:
        if proc.returncode is None:
            proc.kill();await proc.wait()
    if proc.returncode:
        raise RuntimeError('Desktop input failed')
    return {'window':window}

DESKTOP_LOCK = asyncio.Lock()
DESKTOP_CACHE = None
DESKTOP_UPDATED = 0.0
DESKTOP_SNAPSHOT = ''
DESKTOP_CACHE_LOCK = asyncio.Lock()
DESKTOP_PREVIEW_LOCK = asyncio.Lock()
DESKTOP_MUTATION_LOCK = asyncio.Lock()
DESKTOP_PREVIEW_CACHE = OrderedDict()
DESKTOP_PREVIEW_TTL = 15.0

async def desktop_snapshot(previews=False, context=False, window_id=None):
    args = [sys.executable, str(pathlib.Path(__file__).with_name('desktop.py'))]
    if context:
        args.append('--context')
    elif window_id is not None:
        args.extend(['--preview', window_id])
    elif not previews:
        args.append('--names')
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=6)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    max_output = 32*1024 if window_id is not None else 3*1024*1024
    if proc.returncode or len(out) > max_output:
        raise RuntimeError('Desktop windows are unavailable')
    result = json.loads(out)
    if result.get('error'):
        raise RuntimeError(result['error'])
    return result

async def desktop_control(command):
    global DESKTOP_CACHE, DESKTOP_UPDATED, DESKTOP_SNAPSHOT
    if not isinstance(command, dict):
        raise RuntimeError('Invalid desktop command')
    action = command.get('action')
    if action == 'list':
        async with DESKTOP_CACHE_LOCK:
            if DESKTOP_CACHE is None or time.monotonic()-DESKTOP_UPDATED >= 1.0:
                DESKTOP_CACHE = await desktop_snapshot(previews=False)
                DESKTOP_UPDATED = time.monotonic()
                DESKTOP_SNAPSHOT = secrets.token_urlsafe(9)
                DESKTOP_PREVIEW_CACHE.clear()
            result = dict(DESKTOP_CACHE)
            result['snapshot'] = DESKTOP_SNAPSHOT
            return result
    if action == 'preview':
        window = command.get('window')
        snapshot = command.get('snapshot')
        if (not isinstance(window, str) or not re.fullmatch(r'[0-9]{1,10}', window) or
                not isinstance(snapshot, str) or len(snapshot) > 32):
            raise RuntimeError('Invalid window selection')
        async with DESKTOP_CACHE_LOCK:
            if DESKTOP_CACHE is None or time.monotonic()-DESKTOP_UPDATED >= 1.0:
                DESKTOP_CACHE = await desktop_snapshot(previews=False)
                DESKTOP_UPDATED = time.monotonic()
                DESKTOP_SNAPSHOT = secrets.token_urlsafe(9)
                DESKTOP_PREVIEW_CACHE.clear()
            if snapshot != DESKTOP_SNAPSHOT:
                raise RuntimeError('Applications changed. Refresh the list.')
            if window not in {item['id'] for item in DESKTOP_CACHE['windows']}:
                raise RuntimeError('That window has closed. Refresh the list.')
            cached = DESKTOP_PREVIEW_CACHE.get(window)
            if cached and time.monotonic()-cached[0] < DESKTOP_PREVIEW_TTL:
                DESKTOP_PREVIEW_CACHE.move_to_end(window)
                return {'snapshot':snapshot,'window':window,'preview':cached[1]}
        async with DESKTOP_PREVIEW_LOCK:
            result = await desktop_snapshot(window_id=window)
        preview = result.get('preview')
        async with DESKTOP_CACHE_LOCK:
            if snapshot == DESKTOP_SNAPSHOT:
                DESKTOP_PREVIEW_CACHE[window] = (time.monotonic(), preview)
                DESKTOP_PREVIEW_CACHE.move_to_end(window)
                while len(DESKTOP_PREVIEW_CACHE) > 24:
                    DESKTOP_PREVIEW_CACHE.popitem(last=False)
        return {'snapshot':snapshot,'window':window,'preview':preview}
    if action != 'focus' or not isinstance(command.get('window'), str) or not re.fullmatch(r'[0-9]{1,10}', command['window']):
        raise RuntimeError('Invalid window selection')
    async with DESKTOP_MUTATION_LOCK:
        inventory = await desktop_snapshot(previews=False)
        if command['window'] not in {w['id'] for w in inventory['windows']}:
            raise RuntimeError('That window has closed. Choose another window.')
        proc = await asyncio.create_subprocess_exec('xdotool','windowactivate','--sync',command['window'],
            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(),timeout=2)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode:
            raise RuntimeError('Could not focus that window')
        async with DESKTOP_CACHE_LOCK:
            DESKTOP_CACHE = None
            DESKTOP_PREVIEW_CACHE.clear()
        return {'focused':command['window'],'context':await desktop_context(fresh=True)}


async def mouse_control(command):
    """Accept only bounded relative movement and complete clicks on the local X11 desktop."""
    if not isinstance(command, dict):
        raise RuntimeError("Invalid mouse command")
    action = command.get("action")
    if action == "move":
        dx, dy = command.get("dx"), command.get("dy")
        if any(type(v) is not int or abs(v) > 500 for v in (dx, dy)):
            raise RuntimeError("Invalid mouse movement")
        args = ["mousemove_relative", "--", str(dx), str(dy)]
    elif action == "click" and command.get("button") in ("left", "right"):
        args = ["click", "1" if command["button"] == "left" else "3"]
    else:
        raise RuntimeError("Invalid mouse command")
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or not os.environ.get("DISPLAY"):
        raise RuntimeError("Trackpad needs an X11 desktop session")
    try:
        proc = await asyncio.create_subprocess_exec("xdotool", *args,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except FileNotFoundError:
        raise RuntimeError("Install xdotool on the laptop to use the trackpad") from None
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise RuntimeError("Cannot control the laptop pointer; check its X11 session")


async def handler(ws):
    credentials = getattr(ws, 'phonemic_auth', (None, None))
    if not AUTH.valid(*credentials):
        await ws.close(4401, 'Pair this browser')
        return
    if len(ACTIVE_CONNECTIONS) >= 4:
        await ws.close(1013, 'Too many connections')
        return
    ACTIVE_CONNECTIONS.add(ws)
    try:
        await handle_authenticated(ws)
    finally:
        ACTIVE_CONNECTIONS.discard(ws)


async def handle_authenticated(ws):
    # Small frames arrive continuously; Nagle would batch them into extra delay.
    try:
        sock = ws.transport.get_extra_info("socket")
        if sock:
            import socket as _s
            sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
    except Exception:
        pass
    print("paired browser connected", flush=True)
    ff = spawn_sink(RATE)
    rate = RATE
    codec = 'pcm'
    framing = 'pcm'
    audio_version = 2
    decoder = None
    active_stream = None
    expected_seq = 0
    dictation = Dictation()
    herdr = Herdr()
    state = {"n": 0}
    playback_until = 0.0
    task = asyncio.create_task(report(ws, state))
    herdr_task = asyncio.create_task(report_herdr(ws, herdr))
    desktop_task = asyncio.create_task(report_desktop(ws))
    guard = asyncio.create_task(guard_session(ws))
    controls, audio = Budget(40, 80), Budget(192000, 2*1024*1024)
    preview_task = None
    desktop_budget = Budget(1, 3)
    preview_budget = Budget(2, 4)
    desktop_tasks = set()
    pointer_budget = Budget(120, 240)
    t0 = time.time()

    async def send_desktop_result(request_id, command):
        try:
            result = await desktop_control(command)
            if AUTH.valid(*ws.phonemic_auth):
                await ws.send(json.dumps({'type':'desktop','id':request_id,'result':result}))
        except Exception as error:
            if AUTH.valid(*ws.phonemic_auth):
                message = str(error) if isinstance(error, RuntimeError) else 'Desktop windows unavailable'
                await ws.send(json.dumps({'type':'desktop','id':request_id,'error':message}))

    def remember_desktop_task(task):
        desktop_tasks.add(task)
        task.add_done_callback(desktop_tasks.discard)

    try:
        async for msg in ws:
            if isinstance(msg, str):
                if not AUTH.valid(*ws.phonemic_auth):
                    await ws.close(4401, 'Browser access revoked or expired')
                    break
                if len(msg) > 100000:
                    await ws.close(1008, 'Control message limit exceeded')
                    break
                # The phone announces its chosen quality before sending audio.
                try:
                    cfg = json.loads(msg)
                except Exception:
                    continue
                if not isinstance(cfg, dict):
                    continue
                budget = pointer_budget if 'mouse' in cfg else controls
                if not budget.take():
                    kind = 'mouse' if 'mouse' in cfg else 'desktop' if 'desktop' in cfg else 'herdr' if 'herdr' in cfg else 'dictation'
                    await ws.send(json.dumps({'type':kind,'id':cfg.get('id'),'error':'Input is arriving too quickly. Try again.'}))
                    continue
                if cfg.get('audio_v') in (2, 3) or 'audio_codec' in cfg:
                    requested = cfg.get('audio_codec', cfg.get('codec', 'pcm'))
                    if requested not in ('opus', 'pcm'):
                        await ws.send(json.dumps({'type':'audio-capabilities','v':2,'id':cfg.get('id'),'codec':'pcm','framing':'pcm','error':'Unsupported audio codec'}))
                        continue
                    if active_stream is not None:
                        await ws.send(json.dumps({'type':'audio-capabilities','v':audio_version,'id':cfg.get('id'),'codec':codec,'framing':framing,'error':'Audio stream is active'}))
                        continue
                    requested_version = cfg.get('audio_v', 2)
                    requested_framing = cfg.get('framing', 'bare' if requested_version == 3 else 'header')
                    if requested_framing not in ('bare', 'header', 'pcm'):
                        await ws.send(json.dumps({'type':'audio-capabilities','v':2,'id':cfg.get('id'),'codec':'pcm','framing':'pcm','error':'Unsupported audio framing'}))
                        continue
                    if decoder:
                        decoder.close();decoder=None
                    selected_codec = 'opus' if requested == 'opus' and opus_library() else 'pcm'
                    r = cfg.get('rate', rate)
                    if selected_codec == 'opus' and requested_framing in ('bare', 'header'):
                        selected_rate = 48000
                        selected_framing = 'bare' if requested_version == 3 and requested_framing == 'bare' else 'header'
                        selected_version = 3 if selected_framing == 'bare' else 2
                    elif type(r) is int and 8000 <= r <= 48000:
                        selected_codec = 'pcm'
                        selected_rate = r
                        selected_framing = 'pcm'
                        selected_version = 2
                    else:
                        await ws.send(json.dumps({'type':'audio-capabilities','v':2,'id':cfg.get('id'),'codec':'pcm','framing':'pcm','error':'Invalid sample rate'}))
                        continue
                    codec,rate,framing,audio_version=selected_codec,selected_rate,selected_framing,selected_version
                    stop_proc(ff);ff=spawn_sink(rate)
                    print(f"audio -> {codec}/{framing} ({rate} Hz)", flush=True)
                    await ws.send(json.dumps({'type':'audio-capabilities','v':audio_version,'id':cfg.get('id'),'codec':codec,
                                              'framing':framing,'rate':rate,'channels':1,
                                              'packet_samples':960 if codec=='opus' else 0}))
                    continue
                if 'audio_start' in cfg:
                    start = cfg['audio_start']
                    stream = start.get('stream') if isinstance(start,dict) else None
                    start_framing = start.get('framing') if isinstance(start,dict) else None
                    start_rate = start.get('rate') if isinstance(start,dict) else None
                    if (codec != 'opus' or active_stream is not None or type(stream) is not int
                            or not 0 < stream <= 2**32-1 or start_framing != framing or start_rate != rate):
                        await ws.send(json.dumps({'type':'audio-ready','v':audio_version,'stream':stream,'error':'Opus stream unavailable'}))
                        continue
                    try:
                        next_decoder=OpusDecoder()
                    except RuntimeError as error:
                        await ws.send(json.dumps({'type':'audio-ready','v':audio_version,'stream':stream,'error':str(error)}))
                        continue
                    decoder=next_decoder;active_stream=stream;expected_seq=0
                    await ws.send(json.dumps({'type':'audio-ready','v':audio_version,'stream':stream,'codec':'opus','framing':framing}))
                    continue
                if 'audio_end' in cfg:
                    end = cfg['audio_end']
                    stream = end.get('stream') if isinstance(end,dict) else None
                    last_seq = end.get('lastSeq') if isinstance(end,dict) else None
                    packet_count = end.get('packetCount') if isinstance(end,dict) else None
                    if (codec != 'opus' or stream != active_stream or type(stream) is not int
                            or type(last_seq) is not int or type(packet_count) is not int
                            or last_seq != expected_seq-1 or packet_count != expected_seq):
                        await ws.send(json.dumps({'type':'audio-ended','v':audio_version,'stream':stream,'error':'Invalid Opus stream completion'}))
                        continue
                    if decoder:
                        decoder.close();decoder=None
                    active_stream=None
                    await ws.send(json.dumps({'type':'audio-ended','v':audio_version,'stream':stream}))
                    continue
                if "desktop" in cfg:
                    try:
                        command = cfg["desktop"]
                        if isinstance(command,dict) and command.get('action') == 'input':
                            result = await route_desktop_input(command, herdr)
                            await ws.send(json.dumps({"type":"desktop","id":cfg.get("id"),"result":result}))
                            continue
                        action = command.get('action') if isinstance(command,dict) else None
                        if action == 'preview' and not preview_budget.take():
                            raise RuntimeError('Previews are loading too quickly. Try again.')
                        if action != 'preview' and not desktop_budget.take():
                            raise RuntimeError("Wait before requesting more previews")
                        if action in ('list','preview'):
                            if len(desktop_tasks) >= 2:
                                raise RuntimeError('Applications are still loading. Try again.')
                            remember_desktop_task(asyncio.create_task(send_desktop_result(cfg.get('id'), command)))
                            continue
                        result = await desktop_control(cfg["desktop"])
                        if AUTH.valid(*ws.phonemic_auth):
                            await ws.send(json.dumps({"type":"desktop","id":cfg.get("id"),"result":result}))
                    except Exception as error:
                        message = str(error) if isinstance(error,RuntimeError) else "Desktop control unavailable"
                        await ws.send(json.dumps({"type":"desktop","id":cfg.get("id"),"error":message}))
                    continue
                if "mouse" in cfg:
                    try:
                        await mouse_control(cfg["mouse"])
                        await ws.send(json.dumps({"type": "mouse"}))
                    except Exception as error:
                        message = str(error) if isinstance(error, RuntimeError) else "Laptop mouse unavailable"
                        await ws.send(json.dumps({"type": "mouse", "error": message}))
                    continue
                if "herdr" in cfg and "dictation" not in cfg:
                    try:
                        if not isinstance(cfg["herdr"], dict):
                            raise RuntimeError("Invalid terminal command")
                        if cfg["herdr"].get('action') not in ('list','read'):
                            await require_desktop(cfg.get('window'), True)
                        result = await herdr.control(cfg["herdr"])
                        await ws.send(json.dumps({"type": "herdr", "id": cfg.get("id"), "result": result}))
                    except Exception as error:
                        message = str(error) if isinstance(error, RuntimeError) else "Herdr is unavailable on the laptop"
                        await ws.send(json.dumps({"type": "herdr", "id": cfg.get("id"), "error": message}))
                    continue
                if "dictation" in cfg:
                    try:
                        action = cfg["dictation"]
                        started_at = time.monotonic()
                        print(f"dictation {action if action in ('start','stop','abort') else 'invalid'} requested",flush=True)
                        if action == "start":
                            async with asyncio.timeout(8):
                                # Recording uses the already-focused app; it must not wait for
                                # desktop discovery or move focus to a Herdr pane.
                                await dictation.start()
                        elif action in ("stop", "abort"):
                            if action == "stop":
                                await asyncio.sleep(max(0, playback_until-time.monotonic()))
                            await dictation.finish(abort=action == "abort")
                        else:
                            raise RuntimeError("Unknown dictation action")
                        print(f"dictation {action} ready after {time.monotonic()-started_at:.2f}s",flush=True)
                        await ws.send(json.dumps({"type": "dictation", "action": action,"id":cfg.get("id")}))
                    except Exception as error:
                        print(f"dictation request failed ({type(error).__name__})",flush=True)
                        message = str(error) if isinstance(error, RuntimeError) else "Laptop dictation unavailable; check that its updated daemon is running"
                        await ws.send(json.dumps({"type": "dictation", "error": message,"id":cfg.get("id")}))
                    continue
                r = cfg.get("rate", rate)
                if type(r) is not int or not 8000 <= r <= 48000:
                    await ws.close(1008, "Invalid sample rate")
                    break
                if r != rate and 8000 <= r <= 48000:
                    rate = r
                    stop_proc(ff)
                    ff = spawn_sink(rate)
                    print(f"rate -> {rate} Hz", flush=True)
                continue
            if isinstance(msg, bytes) and ff.stdin:
                if codec == 'opus':
                    if framing == 'bare':
                        if not 1 <= len(msg) <= 1275:
                            await ws.close(1008, "Invalid Opus frame size")
                            break
                        stream = active_stream
                        sequence = expected_seq
                        payload = msg
                    else:
                        if len(msg) < 13 or len(msg) > 12+1275 or msg[:3] != b'PM\x02' or msg[3] != 1:
                            await ws.close(1008, "Invalid Opus frame")
                            break
                        stream = int.from_bytes(msg[4:8], 'big')
                        sequence = int.from_bytes(msg[8:12], 'big')
                        payload = msg[12:]
                    if decoder is None or stream != active_stream or sequence != expected_seq:
                        await ws.close(1008, "Unexpected Opus frame")
                        break
                    try:
                        pcm, samples = decoder.decode(payload)
                    except RuntimeError as error:
                        await ws.close(1008, str(error))
                        break
                    if not audio.take(len(pcm)):
                        await ws.close(1008, "Audio rate limit exceeded")
                        break
                    expected_seq += 1
                else:
                    if len(msg) % 2 or not audio.take(len(msg)):
                        await ws.close(1008, "Audio rate limit exceeded")
                        break
                    pcm, samples = msg, len(msg)//2
                # Unflushed, Python holds ~8 KB before writing -- about 170 ms
                # of delay at these rates, for nothing.
                playback_until = max(time.monotonic(), playback_until) + samples/rate
                await asyncio.to_thread(write_audio, ff, pcm)
                state["n"] += len(pcm)
    except Exception as e:
        print(f"stream ended ({type(e).__name__})", flush=True)
    finally:
        task.cancel()
        herdr_task.cancel()
        desktop_task.cancel()
        for task in list(desktop_tasks):
            task.cancel()
        if desktop_tasks:
            await asyncio.gather(*desktop_tasks,return_exceptions=True)
        guard.cancel()
        await asyncio.gather(task, herdr_task, desktop_task, guard, return_exceptions=True)
        await dictation.close()
        if decoder:
            decoder.close()
        stop_proc(ff)
        print(f"connection closed (code={ws.close_code if hasattr(ws, 'close_code') else None})", flush=True)
        print(f"phone disconnected after {time.time()-t0:.0f}s "
              f"({state['n']/2/rate:.1f}s of audio)", flush=True)

async def main():
    ctx = None
    if CERT and KEY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(serve(handler, BIND, PORT, ssl=ctx,
            process_request=process_request, max_size=512*1024, max_queue=8, compression=None, ping_interval=20, ping_timeout=30))
        if LOCAL_BIND:
            if not canonical_origin(LOCAL_URL) or not LOCAL_URL.startswith("https://"):
                raise RuntimeError("Laptop listener requires a configured HTTPS origin")
            local_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            local_ctx.load_cert_chain(LOCAL_CERT, LOCAL_KEY)
            try:
                await stack.enter_async_context(serve(handler, LOCAL_BIND, LOCAL_PORT, ssl=local_ctx,
                    process_request=process_request, max_size=512*1024, max_queue=8, compression=None, ping_interval=20, ping_timeout=30))
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
