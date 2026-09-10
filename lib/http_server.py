"""HTTP authentication, assets, and WebSocket upgrade routing."""

import base64
import hashlib
import json
import os
import re
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from websockets.http11 import Response
from websockets.datastructures import Headers


class Budget:
    def __init__(self, rate, capacity):
        self.rate, self.capacity = rate, capacity
        self.tokens, self.last = capacity, time.monotonic()

    def take(self, amount=1):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + max(0, now - self.last) * self.rate)
        self.last = now
        if amount > self.tokens:
            return False
        self.tokens -= amount
        return True


def reply(status, body='', ctype='text/plain; charset=utf-8', headers=None):
    raw = body.encode()
    reasons = {200: 'OK', 303: 'See Other', 400: 'Bad Request', 401: 'Unauthorized',
               403: 'Forbidden', 404: 'Not Found', 429: 'Too Many Requests',
               503: 'Service Unavailable'}
    return Response(status, reasons[status], Headers({
        'Content-Type': ctype, 'Content-Length': str(len(raw)), **(headers or {})}), raw)


def asset(runtime, name, ctype):
    try:
        body = (runtime.ASSETS / name).read_bytes()
    except Exception:
        return Response(404, 'Not Found', Headers({'Content-Type': 'text/plain'}), b'missing\n')
    return Response(200, 'OK', Headers({
        'Content-Type': ctype, 'Content-Length': str(len(body)),
        'Cache-Control': 'public, max-age=86400'}), body)


def manifest():
    body = json.dumps({
        'name': 'PhoneMic', 'short_name': 'PhoneMic',
        'description': 'Use this phone as a microphone for your computer.',
        'start_url': '/', 'scope': '/', 'display': 'standalone',
        'orientation': 'portrait', 'background_color': '#000000',
        'theme_color': '#000000',
        'icons': [
            {'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
            {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
            {'src': '/icon-maskable-512.png', 'sizes': '512x512',
             'type': 'image/png', 'purpose': 'maskable'},
        ],
    }).encode()
    return Response(200, 'OK', Headers({
        'Content-Type': 'application/manifest+json', 'Content-Length': str(len(body))}), body)


def origins(runtime):
    values = [os.environ.get('PM_PUBLIC_URL', ''), runtime.LOCAL_URL]
    values += os.environ.get('PM_ORIGINS', '').split(',')
    values += [f'http://localhost:{runtime.PORT}', f'http://127.0.0.1:{runtime.PORT}']
    return {origin for value in values if (origin := runtime.canonical_origin(value.strip()))}


def request_origin(runtime, request):
    host = request.headers.get('Host', '')
    candidates = {origin for origin in origins(runtime) if urlsplit(origin).netloc == host.lower()}
    supplied = request.headers.get('Origin')
    if supplied is not None:
        normalized = runtime.canonical_origin(supplied)
        return normalized if normalized in candidates else None
    return candidates.pop() if len(candidates) == 1 else None


def cookie_token(runtime, request):
    value = request.headers.get('Cookie', '')
    if len(value) > 4096 or sum(part.strip().split('=', 1)[0] == runtime.COOKIE
                                for part in value.split(';')) != 1:
        return None
    try:
        parsed = SimpleCookie(value)
        return parsed[runtime.COOKIE].value if runtime.COOKIE in parsed else None
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
    hashes = ' '.join("'sha256-" + base64.b64encode(hashlib.sha256(code.encode()).digest()).decode()
                      + "'" for code in scripts)
    response.headers['Content-Security-Policy'] = (
        "default-src 'none'; script-src " + ((hashes + ' blob:') if hashes else "'none'") +
        "; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
        "worker-src 'self' blob:; media-src 'self' blob:; manifest-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'" )
    return response


def session_cookie(runtime, token):
    return (f'{runtime.COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; '
            f'Max-Age={runtime.SESSION_SECONDS}')


DEBUG_EVENTS = set('page-loaded javascript-error unhandled-rejection connection-opening '
                   'connection-ready connection-closed connection-error connection-teardown '
                   'microphone-acquired audio-running recording-request recording-streaming '
                   'recording-progress recording-release recording-error startup-buffer-full '
                   'audio-backpressure audio-negotiation-failed opus-error opus-unavailable '
                   'dictation-request dictation-ready dictation-error dictation-timeout visibility '
                   'desktop-request desktop-ready desktop-error control-request control-error'.split())


def receive_diagnostics(runtime, request):
    raw = request.headers.get('X-PhoneMic-Diagnostics', '')
    if not raw or len(raw) > 12000:
        return reply(400, 'Invalid diagnostics')
    try:
        events = json.loads(raw)
    except ValueError:
        return reply(400, 'Invalid diagnostics')
    if not isinstance(events, list) or not 1 <= len(events) <= 8:
        return reply(400, 'Invalid diagnostics')
    if not runtime.DEBUG_BUDGET.take(len(events)):
        return reply(429, 'Retry diagnostics later')
    numbers = {'seq', 'time_ms', 'request_id', 'socket', 'code', 'queued_samples',
               'buffered_bytes', 'sent_bytes', 'received_bytes', 'line', 'column'}
    flags = {'ready', 'starting', 'talking', 'capturing', 'hidden', 'online', 'dictation'}
    enums = {
        'action': {'start', 'stop', 'abort', 'unknown', 'key', 'text', 'command', 'focus',
                   'list', 'scroll', 'split', 'close', 'workspace', 'read'},
        'audio_state': {'none', 'running', 'suspended', 'closed', 'interrupted'},
        'app_mode': {'herdr', 'generic', 'none'},
        'error': {'Error', 'TypeError', 'ReferenceError', 'SyntaxError', 'RangeError',
                  'DOMException', 'NotAllowedError', 'NotFoundError', 'NotReadableError',
                  'NotSupportedError', 'AbortError', 'InvalidStateError'},
    }
    for entry in events:
        if not isinstance(entry, dict) or not isinstance(entry.get('event'), str) \
                or entry['event'] not in DEBUG_EVENTS:
            continue
        clean = {'event': entry['event']}
        for key, value in entry.items():
            if key in numbers and type(value) is int and 0 <= value <= 10**15:
                clean[key] = value
            elif key in flags and type(value) is bool:
                clean[key] = value
            elif key in enums and isinstance(value, str) and value in enums[key]:
                clean[key] = value
            elif key == 'client' and isinstance(value, str) and re.fullmatch(r'[a-z0-9]{1,12}', value):
                clean[key] = value
        print('phone-debug ' + json.dumps(clean, separators=(',', ':')), flush=True)
    return reply(200)


def route_request(runtime, conn, request):
    origin = request_origin(runtime, request)
    if origin is None:
        return reply(403, 'Unrecognized origin')
    path = urlsplit(request.path)
    base = path.path
    if request.headers.get('Sec-Fetch-Site') == 'cross-site' \
            and (base in ('/ws', '/diagnostics') or base.startswith('/auth/')):
        return reply(403, 'Cross-site request denied')
    if path.query:
        return reply(303, headers={'Location': '/'}) if base == '/' \
            else reply(400, 'Query credentials are not supported')
    if base == '/sw.js':
        return Response(200, 'OK', Headers({
            'Content-Type': 'text/javascript', 'Service-Worker-Allowed': '/'}), runtime.SW)
    if base in ('/icon-192.png', '/icon-512.png', '/icon-maskable-512.png', '/apple-touch-icon.png'):
        return asset(runtime, base[1:], 'image/png')
    if base == '/manifest.webmanifest':
        return manifest()
    token = cookie_token(runtime, request)
    if base == '/auth/pair':
        code = request.headers.get('X-PhoneMic-Pairing')
        ticket = request.headers.get('X-PhoneMic-Handoff')
        if bool(code) == bool(ticket) or len(code or ticket or '') > 128:
            return reply(400, 'Pairing header required')
        if not runtime.PAIR_REQUEST_BUDGET.take():
            return reply(429, 'Wait before retrying pairing')
        session = runtime.AUTH.pair(code, origin) if code else runtime.AUTH.redeem(ticket, origin)
        if session:
            print('Browser paired' if code else 'Browser connection switched', flush=True)
        return reply(200, headers={'Set-Cookie': session_cookie(runtime, session)}) \
            if session else reply(401, 'Pairing failed')
    authenticated = runtime.AUTH.valid(token, origin)
    if base == '/':
        if not authenticated:
            return reply(200, runtime.PAIR_PAGE, 'text/html; charset=utf-8')
        config = json.dumps({'local': runtime.LOCAL_URL,
                             'public': os.environ.get('PM_PUBLIC_URL', '')}).replace('<', '\\u003c')
        return reply(200, runtime.PAGE.replace('__CONNECTION_CONFIG__', config),
                     'text/html; charset=utf-8')
    if not authenticated:
        return reply(401, 'Pair this browser on the computer')
    if base == '/diagnostics':
        return receive_diagnostics(runtime, request)
    if base == '/auth/status':
        return reply(200)
    if base == '/auth/handoff':
        target = runtime.canonical_origin(request.headers.get('X-PhoneMic-Target', ''))
        if target not in origins(runtime) or target == origin:
            return reply(400, 'Invalid connection target')
        ticket = runtime.AUTH.handoff(token, origin, target)
        return reply(200, json.dumps({'ticket': ticket}), 'application/json') if ticket else reply(401)
    if base == '/ws':
        if not request.headers.get('Origin'):
            return reply(403, 'WebSocket Origin required')
        conn.phonemic_auth = (token, origin)
        return None
    return reply(404, 'Not found')


def process_request(runtime, conn, request):
    try:
        response = route_request(runtime, conn, request)
    except Exception:
        response = reply(503, 'Authentication unavailable')
    return secure_response(response) if response is not None else None


async def process_request_async(runtime, conn, request):
    return await runtime.asyncio.to_thread(process_request, runtime, conn, request)
