#!/usr/bin/env python3
"""PhoneMic receiver entry point and compatibility surface.

The implementation lives in focused modules; this file keeps configuration,
the service entry point, and the small public surface used by tests/tools.
"""

import asyncio
import os
import pathlib
import signal
import ssl
import sys
import time
from collections import OrderedDict
from contextlib import AsyncExitStack

from websockets.asyncio.server import serve

try:
    from . import audio as _audio
    from . import desktop_control as _desktop
    from . import http_server as _http
    from . import session as _session
    from . import dictation as _dictation
    from .herdr import Herdr
    from .phone_page import PAGE, PAIR_PAGE, SW
    from .webauth import AuthStore, COOKIE, SESSION_SECONDS, canonical_origin
except ImportError:
    import audio as _audio
    import desktop_control as _desktop
    import http_server as _http
    import session as _session
    import dictation as _dictation
    from herdr import Herdr
    from phone_page import PAGE, PAIR_PAGE, SW
    from webauth import AuthStore, COOKIE, SESSION_SECONDS, canonical_origin


SINK = os.environ.get('PM_SINK', 'phonemic2')
SRC = os.environ.get('PM_SRC', SINK + '_src')
AUTH = AuthStore()
ACTIVE_CONNECTIONS = set()
PORT = int(os.environ.get('PM_PORT', '8444'))
BIND = os.environ.get('PM_BIND', '127.0.0.1')
CERT = os.environ.get('PM_CERT', '')
KEY = os.environ.get('PM_KEY', '')
LOCAL_URL = os.environ.get('PM_LOCAL_URL', '')
LOCAL_BIND = os.environ.get('PM_LOCAL_BIND', '')
LOCAL_PORT = int(os.environ.get('PM_LOCAL_PORT', '8445'))
LOCAL_CERT = os.environ.get('PM_LOCAL_CERT', '')
LOCAL_KEY = os.environ.get('PM_LOCAL_KEY', '')
ASSETS = pathlib.Path(os.environ.get('PM_ASSETS', pathlib.Path(__file__).resolve().parent.parent / 'assets'))
RATE = int(os.environ.get('PM_RATE', '48000'))
LATENCY = int(os.environ.get('PM_LATENCY_MS', '20'))

CONTEXT_CACHE = None
CONTEXT_UPDATED = 0.0
CONTEXT_LOCK = asyncio.Lock()
DESKTOP_CACHE = None
DESKTOP_UPDATED = 0.0
DESKTOP_LOCK = asyncio.Lock()
DESKTOP_SNAPSHOT = ''
DESKTOP_CACHE_LOCK = asyncio.Lock()
DESKTOP_PREVIEW_LOCK = asyncio.Lock()
DESKTOP_MUTATION_LOCK = asyncio.Lock()
DESKTOP_PREVIEW_CACHE = OrderedDict()
DESKTOP_PREVIEW_TTL = 15.0

Budget = _http.Budget
PAIR_REQUEST_BUDGET = Budget(.5, 10)
DEBUG_BUDGET = Budget(10, 40)
AudioProtocolError = _audio.AudioProtocolError
AudioSession = _audio.AudioSession
OpusDecoder = _audio.OpusDecoder
opus_library = _audio.opus_library
ControlWorker = _desktop.ControlWorker


def process_request(conn, request):
    return _http.process_request(sys.modules[__name__], conn, request)


async def process_request_async(conn, request):
    return await _http.process_request_async(sys.modules[__name__], conn, request)


def route_request(conn, request):
    return _http.route_request(sys.modules[__name__], conn, request)


def receive_diagnostics(request):
    return _http.receive_diagnostics(sys.modules[__name__], request)


async def auth_valid(credentials):
    return await asyncio.to_thread(AUTH.valid, *credentials)


Dictation = _dictation.Dictation


async def desktop_snapshot(previews=True, context=False, window_id=None):
    return await _desktop.desktop_snapshot(sys.modules[__name__], previews, context, window_id)


async def desktop_context(fresh=False):
    return await _desktop.desktop_context(sys.modules[__name__], fresh)


async def require_desktop(window, herdr=None):
    return await _desktop.require_desktop(sys.modules[__name__], window, herdr)


async def generic_input(command, window):
    return await _desktop.generic_input(sys.modules[__name__], command, window)


async def route_desktop_input(message, herdr):
    return await _desktop.route_desktop_input(sys.modules[__name__], message, herdr)


async def desktop_control(command):
    return await _desktop.desktop_control(sys.modules[__name__], command)


async def mouse_control(command):
    return await _desktop.mouse_control(sys.modules[__name__], command)


def listeners():
    return _session.listeners(sys.modules[__name__])


async def guard_session(ws):
    return await _session.guard_session(sys.modules[__name__], ws)


async def report(ws, state):
    return await _session.report(sys.modules[__name__], ws, state)


async def report_desktop(ws):
    return await _session.report_desktop(sys.modules[__name__], ws)


async def report_herdr(ws, herdr):
    return await _session.report_herdr(sys.modules[__name__], ws, herdr)


stop_proc = _session.stop_proc


async def stop_sink(process):
    return await _session.stop_sink(sys.modules[__name__], process)


write_audio = _session.write_audio


def spawn_sink(rate):
    return _session.spawn_sink(sys.modules[__name__], rate)


async def handler(ws):
    return await _session.handler(sys.modules[__name__], ws)


async def handle_authenticated(ws):
    return await _session.handle_authenticated(sys.modules[__name__], ws)


async def main():
    context = None
    if CERT and KEY:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERT, KEY)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(serve(
            handler, BIND, PORT, ssl=context, process_request=process_request_async,
            max_size=512 * 1024, max_queue=8, compression=None, ping_interval=20, ping_timeout=30))
        if LOCAL_BIND:
            if not canonical_origin(LOCAL_URL) or not LOCAL_URL.startswith('https://'):
                raise RuntimeError('Laptop listener requires a configured HTTPS origin')
            local_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            local_context.load_cert_chain(LOCAL_CERT, LOCAL_KEY)
            try:
                await stack.enter_async_context(serve(
                    handler, LOCAL_BIND, LOCAL_PORT, ssl=local_context,
                    process_request=process_request_async, max_size=512 * 1024, max_queue=8,
                    compression=None, ping_interval=20, ping_timeout=30))
                print(f'LAN HTTPS listening on {LOCAL_BIND}:{LOCAL_PORT}', flush=True)
            except OSError:
                print('LAN listener unavailable; public endpoint remains active', flush=True)
        print(f'listening on {BIND}:{PORT}', flush=True)
        await asyncio.get_running_loop().create_future()


if __name__ == '__main__':
    pidfile = os.environ.get('PM_PIDFILE')
    if pidfile:
        pathlib.Path(pidfile).write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
