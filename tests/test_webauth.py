"""Authentication tests use isolated state and fake desktop/audio boundaries."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock

from lib.webauth import AuthStore, COOKIE, SESSION_SECONDS, canonical_origin
from lib import webmic
from websockets.datastructures import Headers
from websockets.http11 import Request
from websockets.asyncio.server import serve
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, ConnectionClosed

PUBLIC = 'https://mic.example.com'
LAN = 'https://lan.mic.example.com'


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.auth = AuthStore(Path(self.temp.name)/'auth.json', clock=lambda:self.now)

    def test_single_pair_and_no_credentials_in_state(self):
        self.assertFalse(self.auth.valid('anything', PUBLIC))
        code = self.auth.new_code()
        self.assertRegex(code, r'^[0-9A-F]{4}-[0-9A-F]{4}$')
        token = self.auth.pair(code, PUBLIC)
        self.assertTrue(self.auth.valid(token, PUBLIC))
        self.assertFalse(self.auth.valid(token, LAN))
        self.assertIsNone(self.auth.pair(code, PUBLIC))
        replacement = self.auth.pair(self.auth.new_code(), PUBLIC)
        self.assertFalse(self.auth.valid(token, PUBLIC))
        self.assertTrue(self.auth.valid(replacement, PUBLIC))
        self.assertNotIn(replacement, self.auth.path.read_text())
        self.assertEqual(self.auth.path.stat().st_mode & 0o777, 0o600)

    def test_code_expiry_attempt_limit_and_session_expiry(self):
        code = self.auth.new_code()
        self.now += 301
        self.assertIsNone(self.auth.pair(code, PUBLIC))
        code = self.auth.new_code()
        for _ in range(10):
            self.assertIsNone(self.auth.pair('wrong', PUBLIC))
        self.assertIsNone(self.auth.pair(code, PUBLIC))
        token = self.auth.pair(self.auth.new_code(), PUBLIC)
        self.now += SESSION_SECONDS + 1
        self.assertFalse(self.auth.valid(token, PUBLIC))

    def test_handoff_is_bound_single_use_and_revocable(self):
        token = self.auth.pair(self.auth.new_code(), PUBLIC)
        ticket = self.auth.handoff(token, PUBLIC, LAN)
        self.assertIsNone(self.auth.redeem(ticket, PUBLIC))
        lan_token = self.auth.redeem(ticket, LAN)
        self.assertTrue(self.auth.valid(lan_token, LAN))
        self.assertTrue(self.auth.valid(token, PUBLIC))
        self.assertIsNone(self.auth.redeem(ticket, LAN))
        ticket = self.auth.handoff(token, PUBLIC, LAN)
        self.now += 31
        self.assertIsNone(self.auth.redeem(ticket, LAN))
        ticket = self.auth.handoff(token, PUBLIC, LAN)
        self.auth.pair(self.auth.new_code(), PUBLIC)
        self.assertIsNone(self.auth.redeem(ticket, LAN))
        self.assertFalse(self.auth.valid(lan_token, LAN))
        self.auth.revoke()
        self.assertFalse(self.auth.status()['paired'])

    def test_concurrent_pairing_only_one_wins(self):
        code = self.auth.new_code()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.auth.pair(code, PUBLIC), range(2)))
        self.assertEqual(sum(bool(t) for t in results), 1)

    def test_corrupt_state_fails_closed(self):
        token = self.auth.pair(self.auth.new_code(), PUBLIC)
        self.auth.path.write_text('broken')
        self.assertFalse(self.auth.valid(token, PUBLIC))
        with self.assertRaises(ValueError):
            self.auth.new_code()


class HttpTests(StoreTests):
    def setUp(self):
        super().setUp()
        self.patchers = [patch.object(webmic, 'AUTH', self.auth),
            patch.object(webmic, 'LOCAL_URL', LAN),
            patch.dict(webmic.os.environ, {'PM_PUBLIC_URL': PUBLIC, 'PM_ORIGINS':''}),
            patch.object(webmic, 'PAIR_REQUEST_BUDGET', webmic.Budget(.5,10))]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def request(self, path='/', origin=None, token=None, extra=None):
        headers = Headers({'Host':'mic.example.com'})
        if origin is not None:
            headers['Origin'] = origin
        if token:
            headers['Cookie'] = COOKIE+'='+token
        for key,value in (extra or {}).items():
            headers[key] = value
        conn = SimpleNamespace()
        response = webmic.process_request(conn, Request(path, headers))
        return response, conn

    def test_legacy_tokens_and_missing_credentials_cannot_open_websocket(self):
        for path in ['/ws','/ws?token=anything','/ws?not_token=anything']:
            response,_ = self.request(path, origin=PUBLIC)
            self.assertIn(response.status_code, [400,401])
        response,_ = self.request('/?token=anything')
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['Location'],'/')
        response,_ = self.request()
        self.assertIn(b'Pair this browser',response.body)
        self.assertNotIn(b'id=remote',response.body)

    def test_pair_cookie_headers_and_authenticated_page(self):
        response,_ = self.request('/auth/pair', extra={'X-PhoneMic-Pairing':self.auth.new_code()})
        self.assertEqual(response.status_code,200)
        cookie=response.headers['Set-Cookie']
        for flag in ['Secure','HttpOnly','SameSite=Strict','Path=/']:
            self.assertIn(flag,cookie)
        token=cookie.split(';')[0].split('=',1)[1]
        response,_ = self.request(token=token)
        self.assertIn(b'id=remote',response.body)
        self.assertNotIn(token.encode(),response.body)
        self.assertNotIn(b'?token=',response.body)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(response.headers['Referrer-Policy'],'no-referrer')
        self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
        self.assertIn("'sha256-",response.headers['Content-Security-Policy'])
        response,conn=self.request('/ws',origin=PUBLIC,token=token)
        self.assertIsNone(response)
        self.assertEqual(conn.phonemic_auth,(token,PUBLIC))

    def test_cross_origin_unknown_host_and_missing_websocket_origin_denied(self):
        code=self.auth.new_code()
        response,_=self.request('/auth/pair',origin='https://evil.example',extra={'X-PhoneMic-Pairing':code})
        self.assertEqual(response.status_code,403)
        token=self.auth.pair(code,PUBLIC)
        for origin in ['null','https://evil.example',None]:
            response,_=self.request('/ws',origin=origin,token=token)
            self.assertIn(response.status_code,[401,403])
        response,_=self.request(token=token,extra={'Host':'evil.example'})
        self.assertIn(response.status_code,[403,503])
        response,_=self.request('/auth/handoff',token=token,extra={'X-PhoneMic-Target':'https://evil.example'})
        self.assertEqual(response.status_code,400)

    def test_duplicate_cookies_fail_closed_and_manifest_has_no_secret(self):
        token=self.auth.pair(self.auth.new_code(),PUBLIC)
        response,_=self.request('/ws',origin=PUBLIC,extra={'Cookie':f'{COOKIE}=bad; {COOKIE}={token}'})
        self.assertEqual(response.status_code,401)
        response,_=self.request('/manifest.webmanifest')
        self.assertEqual(json.loads(response.body)['start_url'],'/')
        self.assertNotIn(b'token=',response.body)

    def test_diagnostics_require_pairing_and_strip_sensitive_fields(self):
        payload=json.dumps([{'event':'recording-request','client':'test123','time_ms':12,
                            'token':'secret-value','url':'https://private.invalid/','text':'private words',
                            'action':'start','audio':'secret audio','error':'private words'}])
        response,_=self.request('/diagnostics',extra={'X-PhoneMic-Diagnostics':payload})
        self.assertEqual(response.status_code,401)
        token=self.auth.pair(self.auth.new_code(),PUBLIC)
        with patch('builtins.print') as log:
            response,_=self.request('/diagnostics',token=token,extra={'X-PhoneMic-Diagnostics':payload})
            self.assertEqual(response.status_code,200)
            logged=log.call_args.args[0]
            self.assertIn('recording-request',logged)
            for secret in ('secret-value','private.invalid','private words','secret audio'):
                self.assertNotIn(secret,logged)
        response,_=self.request('/diagnostics',token=token,extra={'Sec-Fetch-Site':'cross-site','X-PhoneMic-Diagnostics':payload})
        self.assertEqual(response.status_code,403)

    def test_pair_endpoint_is_rate_limited(self):
        for _ in range(10):
            self.request('/auth/pair',extra={'X-PhoneMic-Pairing':'bad'})
        response,_=self.request('/auth/pair',extra={'X-PhoneMic-Pairing':'bad'})
        self.assertEqual(response.status_code,429)


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.auth=AuthStore(Path(self.temp.name)/'auth.json')
        self.remote=SimpleNamespace(control=AsyncMock(return_value={'panes':[]}))
        self.patches=[patch.object(webmic,'AUTH',self.auth),patch.object(webmic,'spawn_sink',return_value=MagicMock()),
            patch.object(webmic,'stop_proc'),patch.object(webmic,'Herdr',return_value=self.remote),
            patch.object(webmic,'report_desktop',new=AsyncMock()),patch.object(webmic,'report',new=AsyncMock()),patch.object(webmic,'report_herdr',new=AsyncMock())]
        for p in self.patches:p.start()
        self.server=await serve(webmic.handler,'127.0.0.1',0,process_request=webmic.process_request,max_size=512*1024,compression=None)
        self.port=self.server.sockets[0].getsockname()[1]
        self.port_patch=patch.object(webmic,'PORT',self.port);self.port_patch.start()
        self.origin=f'http://localhost:{self.port}'
        self.token=self.auth.pair(self.auth.new_code(),self.origin)

    async def asyncTearDown(self):
        self.server.close();await self.server.wait_closed()
        self.port_patch.stop()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def client(self, token=None):
        return connect(f'ws://localhost:{self.port}/ws',origin=self.origin,
                       additional_headers={'Cookie':COOKIE+'='+(token or self.token)})

    async def test_dictation_starts_without_desktop_focus_or_herdr(self):
        bridge=SimpleNamespace(start=AsyncMock(),finish=AsyncMock(),close=AsyncMock())
        with patch.object(webmic,'Dictation',return_value=bridge),patch.object(webmic,'require_desktop',side_effect=RuntimeError('No focused window')) as focus:
            async with self.client() as ws:
                await ws.send(json.dumps({'dictation':'start','id':31,'herdr':True,'pane':'old-page-pane'}))
                reply=json.loads(await asyncio.wait_for(ws.recv(),1))
                self.assertEqual(reply,{'type':'dictation','action':'start','id':31})
                bridge.start.assert_awaited_once();focus.assert_not_called();self.remote.control.assert_not_called()
                await ws.send(json.dumps({'dictation':'abort','id':32}))
                self.assertEqual(json.loads(await ws.recv())['id'],32)
                self.assertIsNone(ws.close_code)

    async def test_slow_previews_do_not_block_pointer_replies(self):
        async def slow_preview(command):
            await asyncio.sleep(.5)
            return {'windows':[]}
        with patch.object(webmic,'desktop_control',side_effect=slow_preview),patch.object(webmic,'mouse_control',new=AsyncMock()):
            async with self.client() as ws:
                await ws.send(json.dumps({'desktop':{'action':'list'},'id':1}))
                await ws.send(json.dumps({'mouse':{'action':'move','dx':1,'dy':1}}))
                reply=json.loads(await asyncio.wait_for(ws.recv(),.25))
                self.assertEqual(reply['type'],'mouse')
                self.assertEqual(json.loads(await ws.recv())['id'],1)

    async def test_pointer_burst_does_not_disconnect_or_starve_other_controls(self):
        with patch.object(webmic,'mouse_control',new=AsyncMock()):
            async with self.client() as ws:
                for _ in range(300):
                    await ws.send(json.dumps({'mouse':{'action':'move','dx':1,'dy':1}}))
                for _ in range(300):
                    message=json.loads(await asyncio.wait_for(ws.recv(),2))
                    self.assertEqual(message['type'],'mouse')
                await ws.send(json.dumps({'herdr':{'action':'list'},'id':999}))
                self.assertEqual(json.loads(await ws.recv())['id'],999)
                self.assertIsNone(ws.close_code)

    async def test_anonymous_rejected_without_desktop_access(self):
        with self.assertRaises(InvalidStatus) as error:
            async with self.client('invalid'):pass
        self.assertEqual(error.exception.response.status_code,401)
        self.remote.control.assert_not_awaited()

    async def test_replacement_revokes_live_connection(self):
        async with self.client() as ws:
            await ws.send(json.dumps({'herdr':{'action':'list'},'id':1}))
            self.assertEqual(json.loads(await ws.recv())['result'],{'panes':[]})
            replacement=self.auth.pair(self.auth.new_code(),self.origin)
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(),2)
            self.assertEqual(ws.close_code,4401)
        async with self.client(replacement) as ws:
            self.auth.revoke()
            with self.assertRaises(ConnectionClosed):await asyncio.wait_for(ws.recv(),2)
            self.assertEqual(ws.close_code,4401)

    async def test_oversized_frame_and_invalid_rate_rejected(self):
        async with self.client() as ws:
            await ws.send(b'x'*(512*1024+1))
            with self.assertRaises(ConnectionClosed):await ws.recv()
            self.assertEqual(ws.close_code,1009)
        async with self.client() as ws:
            await ws.send(json.dumps({'rate':'not a rate'}))
            with self.assertRaises(ConnectionClosed):await ws.recv()
            self.assertEqual(ws.close_code,1008)
        self.remote.control.assert_not_awaited()


if __name__=='__main__':unittest.main()
