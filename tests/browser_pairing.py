"""Optional real-browser security checks: run with Python Playwright + Chromium installed."""
import asyncio
import io
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright
from websockets.asyncio.server import serve
from lib import webmic
from lib.webauth import AuthStore, COOKIE


async def main():
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem'),'-subj','/CN=public.test',
            '-addext','subjectAltName=DNS:public.test,DNS:lan.test'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(root/'cert.pem',root/'key.pem')
        auth=AuthStore(root/'auth.json')
        async def control(message):
            if message['action']=='list':
                return {'panes':[{'id':'test-pane','workspace':'Test','focused':True,'state':'idle'}]}
            if message['action']=='read':return {'pane':'test-pane','text':'Test output'}
            return {'pane':'test-pane'}
        import base64
        from PIL import Image
        thumbnail=io.BytesIO()
        Image.new('RGB',(280,176),(30,50,65)).save(thumbnail,format='JPEG')
        preview=base64.b64encode(thumbnail.getvalue()).decode()
        context={'id':'456','app':'Herdr terminal','herdr':True}
        async def current_context(**kw):return dict(context)
        inputs=[]
        async def generic(command,window):
            inputs.append((command,window));return {}
        async def start_dictation():await asyncio.sleep(5)
        async def finish_dictation(**kw):pass
        async def close_dictation():pass
        focused=[]
        async def desktop(command):
            if command['action']=='focus':
                focused.append(command['window']);context.update(id=command['window'],app='Editor',herdr=False);return {'focused':command['window'],'context':dict(context)}
            return {'windows':[{'id':'123','app':'Editor','title':'Project notes','active':False,'preview':preview},
                               {'id':'456','app':'Browser','title':'Documentation','active':True,'preview':None}]}
        with patch.object(webmic,'Dictation',return_value=SimpleNamespace(start=start_dictation,finish=finish_dictation,close=close_dictation)),patch.object(webmic,'desktop_context',side_effect=current_context),patch.object(webmic,'generic_input',side_effect=generic),patch.object(webmic,'desktop_control',side_effect=desktop),patch.object(webmic,'AUTH',auth),patch.object(webmic,'listeners',return_value=[]),\
             patch.object(webmic,'Herdr',return_value=SimpleNamespace(control=control)),\
             patch.object(webmic,'spawn_sink',side_effect=lambda rate:SimpleNamespace(stdin=io.BytesIO())),\
             patch.object(webmic,'stop_proc'):
            async with serve(webmic.handler,'127.0.0.1',0,ssl=tls,process_request=webmic.process_request,
                             max_size=512*1024,max_queue=8,compression=None) as server:
                port=server.sockets[0].getsockname()[1]
                public=f'https://public.test:{port}';lan=f'https://lan.test:{port}'
                with patch.object(webmic,'LOCAL_URL',lan),patch.dict(os.environ,{'PM_PUBLIC_URL':public}):
                    async with async_playwright() as p:
                        browser=await p.chromium.launch(args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream','--no-proxy-server',
                            '--host-resolver-rules=MAP public.test 127.0.0.1, MAP lan.test 127.0.0.1'])
                        first=await browser.new_context(ignore_https_errors=True,viewport={'width':390,'height':844})
                        tab=await first.new_page();errors=[]
                        tab.on('pageerror',lambda error:errors.append(str(error)))
                        await tab.goto(public+'/?token=old-token')
                        assert tab.url==public+'/'
                        assert await tab.locator('#pair-form').is_visible()
                        await tab.locator('#code').fill(auth.new_code());await tab.locator('#submit').click()
                        await tab.locator('#remote').wait_for(state='visible')
                        await tab.locator('#picked-name').filter(has_text='Test').wait_for()
                        await tab.locator('#open-apps').click()
                        await tab.locator('.app-card').first.wait_for()
                        assert await tab.locator('.app-card').count()==2
                        await tab.locator('.app-preview').first.evaluate('e=>e.decode()')
                        assert await tab.locator('.app-preview').first.evaluate('e=>e.naturalWidth')==280
                        assert await tab.locator('#apps-grid').evaluate('e=>getComputedStyle(e).gridTemplateColumns.split(" ").length')==2
                        await tab.locator('.app-card').first.click()
                        await tab.locator('#apps-status').filter(has_text='Window focused.').wait_for()
                        assert focused==['123']
                        assert await tab.locator('#apps-dialog').is_visible()
                        assert await tab.locator('.app-card').first.get_attribute('aria-pressed')=='true'
                        await tab.screenshot(path='/tmp/phonemic-apps-modal.png')
                        await tab.locator('#apps-close').click()
                        assert not await tab.locator('#commands-tab').is_visible()
                        assert not await tab.locator('#panes').is_visible()
                        await tab.locator('#composer-tab').click()
                        await tab.locator('#composer').fill('Hello editor')
                        await tab.locator('#insert-text').click()
                        await asyncio.sleep(.2)
                        assert inputs[-1]==({'action':'text','pane':'test-pane','text':'Hello editor'},'123')
                        await tab.screenshot(path='/tmp/phonemic-generic-controls.png')
                        context.update(id='456',app='Herdr terminal',herdr=True)
                        await tab.locator('#panes').wait_for(state='visible')
                        assert await tab.locator('#commands-tab').is_visible()
                        assert not await tab.locator('#apps-dialog').is_visible()
                        cookies=await first.cookies(public)
                        cookie=next(c for c in cookies if c['name']==COOKIE)
                        assert cookie['httpOnly'] and cookie['secure'] and cookie['sameSite']=='Strict'
                        assert COOKIE not in await tab.evaluate('document.cookie')
                        assert cookie['value'] not in await tab.content()
                        await tab.reload();await tab.locator('#picked-name').filter(has_text='Test').wait_for()
                        assert await tab.locator('#remote').is_visible()
                        # AudioWorklet loading must still work under the CSP, without opening a microphone.
                        await tab.evaluate('prepareAudio()')
                        await tab.evaluate('dictation.checked=true;hf.checked=true')
                        await tab.locator('#talk').click()
                        await tab.locator('#st').filter(has_text='Dictating to laptop').wait_for(timeout=10000)
                        await asyncio.sleep(.5)
                        assert await tab.evaluate('ready && talking && sent > 0')
                        await tab.locator('#talk').click()
                        await tab.locator('#st').filter(has_text='Sent to laptop for transcription').wait_for(timeout=10000)
                        assert await tab.evaluate('ready && ws.readyState===1')
                        await tab.locator('#gear').click();await tab.locator('#switch-connection').click()
                        await tab.wait_for_url(lan+'/**')
                        await tab.locator('#remote').wait_for(state='visible')
                        await tab.locator('#picked-name').filter(has_text='Test').wait_for()
                        assert await tab.locator('#remote').is_visible()
                        assert not auth.status()['pairing_pending']
                        # A link opened from another site must recover the existing Strict cookie.
                        await tab.goto(public+'/')
                        await tab.locator('#remote').wait_for(state='visible')
                        await tab.goto(lan+'/')
                        await tab.locator('#remote').wait_for(state='visible')
                        await tab.locator('#gear').click();await tab.locator('#switch-connection').click()
                        await tab.wait_for_url(public+'/**')
                        await tab.locator('#remote').wait_for(state='visible')
                        await tab.locator('#picked-name').filter(has_text='Test').wait_for()
                        second=await browser.new_context(ignore_https_errors=True)
                        replacement=await second.new_page();await replacement.goto(public)
                        assert await replacement.locator('#pair-form').is_visible()
                        await replacement.locator('#code').fill(auth.new_code());await replacement.locator('#submit').click()
                        await replacement.locator('#remote').wait_for(state='visible')
                        await replacement.locator('#picked-name').filter(has_text='Test').wait_for()
                        await tab.locator('#pair-form').wait_for(state='visible',timeout=5000)
                        auth.revoke()
                        await replacement.locator('#pair-form').wait_for(state='visible',timeout=5000)
                        assert not errors,errors
                        await browser.close()
    print('Browser security checks passed: pairing, Secure/HttpOnly cookie, reload, CSP audio worklet, cross-origin handoff, replacement and live revocation.')


if __name__=='__main__':asyncio.run(main())
