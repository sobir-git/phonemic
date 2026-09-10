"""Desktop input, previews, Herdr context, and dictation controls."""

import asyncio
import json
import pathlib
import re
import secrets
import sys
import time


async def desktop_snapshot(runtime, previews=True, context=False, window_id=None):
    args = [sys.executable, str(pathlib.Path(runtime.__file__).with_name('desktop.py'))]
    if context:
        args.append('--context')
    elif window_id is not None:
        args.extend(['--preview', window_id])
    elif not previews:
        args.append('--names')
    proc = await runtime.asyncio.create_subprocess_exec(
        *args, stdout=runtime.asyncio.subprocess.PIPE, stderr=runtime.asyncio.subprocess.DEVNULL)
    try:
        out, _ = await runtime.asyncio.wait_for(proc.communicate(), timeout=6)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    max_output = 32 * 1024 if window_id is not None else 3 * 1024 * 1024
    if proc.returncode or len(out) > max_output:
        raise RuntimeError('Desktop windows are unavailable')
    result = json.loads(out)
    if result.get('error'):
        raise RuntimeError(result['error'])
    return result


async def desktop_context(runtime, fresh=False):
    async with runtime.CONTEXT_LOCK:
        if not fresh and runtime.CONTEXT_CACHE is not None \
                and runtime.time.monotonic() - runtime.CONTEXT_UPDATED < .4:
            return runtime.CONTEXT_CACHE
        runtime.CONTEXT_CACHE = await runtime.desktop_snapshot(context=True)
        runtime.CONTEXT_UPDATED = runtime.time.monotonic()
        return runtime.CONTEXT_CACHE


async def require_desktop(runtime, window, herdr=None):
    current = await runtime.desktop_context(fresh=True)
    if not window or current.get('id') != window \
            or (herdr is not None and current.get('herdr') != herdr):
        raise RuntimeError('Focused app changed. Check the current app and try again.')
    return current


async def generic_input(runtime, command, window):
    if not isinstance(command, dict):
        raise RuntimeError('Invalid input')
    action, data = command.get('action'), None
    if action == 'key':
        keys = {'esc': 'Escape', 'tab': 'Tab', 'left': 'Left', 'right': 'Right', 'up': 'Up',
                'down': 'Down', 'space': 'space', 'backspace': 'BackSpace', 'enter': 'Return',
                'c': 'c', 'u': 'u'}
        key, mods = command.get('key'), command.get('modifiers', [])
        if key not in keys or not isinstance(mods, list) or len(mods) > 2 \
                or any(mod not in ('ctrl', 'alt') for mod in mods):
            raise RuntimeError('Invalid key')
        args = ['key', '--clearmodifiers', '+'.join(mods + [keys[key]])]
    elif action in ('text', 'command'):
        data = command.get('text')
        if not isinstance(data, str) or not data.strip() or len(data) > 20000 \
                or any((ord(char) < 32 and char not in '\n\t') or ord(char) == 127 for char in data):
            raise RuntimeError('Use text without control codes, up to 20000 characters')
        args = ['type', '--clearmodifiers', '--delay', '0', '--file', '-']
        data = data.encode()
    elif action == 'scroll':
        direction = command.get('direction')
        if direction == 'bottom':
            args = ['key', '--clearmodifiers', 'ctrl+End']
        elif direction in ('up', 'down'):
            current = await runtime.require_desktop(window, False)
            args = ['mousemove', '--window', window, str(current['width'] // 2),
                    str(current['height'] // 2), 'click', '--repeat', '3', '--delay', '30',
                    '4' if direction == 'up' else '5']
        else:
            raise RuntimeError('Invalid scroll direction')
    else:
        raise RuntimeError('Unsupported desktop input')
    await runtime.require_desktop(window, False)
    proc = await runtime.asyncio.create_subprocess_exec(
        'xdotool', *args,
        stdin=runtime.asyncio.subprocess.PIPE if data is not None else runtime.asyncio.subprocess.DEVNULL,
        stdout=runtime.asyncio.subprocess.DEVNULL, stderr=runtime.asyncio.subprocess.DEVNULL)
    try:
        await runtime.asyncio.wait_for(proc.communicate(data), timeout=5)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise RuntimeError('Desktop input failed')
    return {'window': window}


async def route_desktop_input(runtime, message, herdr):
    command = message.get('command')
    if not isinstance(command, dict) or command.get('action') not in ('key', 'scroll', 'text', 'command') \
            or type(message.get('herdr')) is not bool:
        raise RuntimeError('Invalid input command')
    current = await runtime.require_desktop(message.get('window'), message['herdr'])
    if current['herdr']:
        return await herdr.control(command)
    return await runtime.generic_input(command, current['id'])


async def desktop_control(runtime, command):
    if not isinstance(command, dict):
        raise RuntimeError('Invalid desktop command')
    action = command.get('action')
    if action == 'list':
        async with runtime.DESKTOP_CACHE_LOCK:
            if runtime.DESKTOP_CACHE is None or runtime.time.monotonic() - runtime.DESKTOP_UPDATED >= 1.0:
                runtime.DESKTOP_CACHE = await runtime.desktop_snapshot(previews=False)
                runtime.DESKTOP_UPDATED = runtime.time.monotonic()
                runtime.DESKTOP_SNAPSHOT = secrets.token_urlsafe(9)
                runtime.DESKTOP_PREVIEW_CACHE.clear()
            result = dict(runtime.DESKTOP_CACHE)
            result['snapshot'] = runtime.DESKTOP_SNAPSHOT
            return result
    if action == 'preview':
        window = command.get('window')
        snapshot = command.get('snapshot')
        if (not isinstance(window, str) or not re.fullmatch(r'[0-9]{1,10}', window) or
                not isinstance(snapshot, str) or len(snapshot) > 32):
            raise RuntimeError('Invalid window selection')
        async with runtime.DESKTOP_CACHE_LOCK:
            if runtime.DESKTOP_CACHE is None or runtime.time.monotonic() - runtime.DESKTOP_UPDATED >= 1.0:
                runtime.DESKTOP_CACHE = await runtime.desktop_snapshot(previews=False)
                runtime.DESKTOP_UPDATED = runtime.time.monotonic()
                runtime.DESKTOP_SNAPSHOT = secrets.token_urlsafe(9)
                runtime.DESKTOP_PREVIEW_CACHE.clear()
            if snapshot != runtime.DESKTOP_SNAPSHOT:
                raise RuntimeError('Applications changed. Refresh the list.')
            if window not in {item['id'] for item in runtime.DESKTOP_CACHE['windows']}:
                raise RuntimeError('That window has closed. Refresh the list.')
            cached = runtime.DESKTOP_PREVIEW_CACHE.get(window)
            if cached and runtime.time.monotonic() - cached[0] < runtime.DESKTOP_PREVIEW_TTL:
                runtime.DESKTOP_PREVIEW_CACHE.move_to_end(window)
                return {'snapshot': snapshot, 'window': window, 'preview': cached[1]}
        async with runtime.DESKTOP_PREVIEW_LOCK:
            result = await runtime.desktop_snapshot(window_id=window)
        preview = result.get('preview')
        async with runtime.DESKTOP_CACHE_LOCK:
            if snapshot == runtime.DESKTOP_SNAPSHOT:
                runtime.DESKTOP_PREVIEW_CACHE[window] = (runtime.time.monotonic(), preview)
                runtime.DESKTOP_PREVIEW_CACHE.move_to_end(window)
                while len(runtime.DESKTOP_PREVIEW_CACHE) > 24:
                    runtime.DESKTOP_PREVIEW_CACHE.popitem(last=False)
        return {'snapshot': snapshot, 'window': window, 'preview': preview}
    if action != 'focus' or not isinstance(command.get('window'), str) \
            or not re.fullmatch(r'[0-9]{1,10}', command['window']):
        raise RuntimeError('Invalid window selection')
    async with runtime.DESKTOP_MUTATION_LOCK:
        inventory = await runtime.desktop_snapshot(previews=False)
        if command['window'] not in {window['id'] for window in inventory['windows']}:
            raise RuntimeError('That window has closed. Choose another window.')
        proc = await runtime.asyncio.create_subprocess_exec(
            'xdotool', 'windowactivate', '--sync', command['window'],
            stdout=runtime.asyncio.subprocess.DEVNULL, stderr=runtime.asyncio.subprocess.DEVNULL)
        try:
            await runtime.asyncio.wait_for(proc.wait(), timeout=2)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode:
            raise RuntimeError('Could not focus that window')
        async with runtime.DESKTOP_CACHE_LOCK:
            runtime.DESKTOP_CACHE = None
            runtime.DESKTOP_PREVIEW_CACHE.clear()
        return {'focused': command['window'], 'context': await runtime.desktop_context(fresh=True)}


async def mouse_control(runtime, command):
    """Accept only bounded relative movement and complete clicks on local X11."""
    if not isinstance(command, dict):
        raise RuntimeError("Invalid mouse command")
    action = command.get("action")
    if action == "move":
        dx, dy = command.get("dx"), command.get("dy")
        if any(type(value) is not int or abs(value) > 500 for value in (dx, dy)):
            raise RuntimeError("Invalid mouse movement")
        args = ["mousemove_relative", "--", str(dx), str(dy)]
    elif action == "click" and command.get("button") in ("left", "right"):
        args = ["click", "1" if command["button"] == "left" else "3"]
    else:
        raise RuntimeError("Invalid mouse command")
    if runtime.os.environ.get("XDG_SESSION_TYPE") == "wayland" or not runtime.os.environ.get("DISPLAY"):
        raise RuntimeError("Trackpad needs an X11 desktop session")
    try:
        proc = await runtime.asyncio.create_subprocess_exec(
            "xdotool", *args, stdout=runtime.asyncio.subprocess.DEVNULL,
            stderr=runtime.asyncio.subprocess.DEVNULL)
    except FileNotFoundError:
        raise RuntimeError("Install xdotool on the laptop to use the trackpad") from None
    try:
        await runtime.asyncio.wait_for(proc.wait(), timeout=2)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise RuntimeError("Cannot control the laptop pointer; check its X11 session")


class ControlWorker:
    """Run ordered non-audio controls without blocking frame reception."""

    def __init__(self, runtime, ws, herdr, dictation):
        self.runtime, self.ws = runtime, ws
        self.herdr, self.dictation = herdr, dictation
        self.queue = runtime.asyncio.Queue(maxsize=256)
        self.desktop_tasks = set()
        self.desktop_budget = runtime.Budget(1, 3)
        self.preview_budget = runtime.Budget(2, 4)

    def enqueue(self, config, playback_until):
        try:
            self.queue.put_nowait((config, playback_until))
            return True
        except self.runtime.asyncio.QueueFull:
            return False

    async def run(self):
        while True:
            config, playback_until = await self.queue.get()
            try:
                await self.handle(config, playback_until)
            except self.runtime.asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"control request failed ({type(error).__name__})", flush=True)
            finally:
                self.queue.task_done()

    async def handle(self, config, playback_until):
        if 'desktop' in config:
            await self._desktop(config)
        elif 'mouse' in config:
            await self._mouse(config)
        elif 'herdr' in config and 'dictation' not in config:
            await self._herdr(config)
        elif 'dictation' in config:
            await self._dictation(config, playback_until)

    async def _desktop(self, config):
        try:
            command = config['desktop']
            if isinstance(command, dict) and command.get('action') == 'input':
                result = await self.runtime.route_desktop_input(command, self.herdr)
                await self.ws.send(json.dumps({'type': 'desktop', 'id': config.get('id'), 'result': result}))
                return
            action = command.get('action') if isinstance(command, dict) else None
            if action == 'preview' and not self.preview_budget.take():
                raise RuntimeError('Previews are loading too quickly. Try again.')
            if action != 'preview' and not self.desktop_budget.take():
                raise RuntimeError('Wait before requesting more previews')
            if action in ('list', 'preview'):
                if len(self.desktop_tasks) >= 2:
                    raise RuntimeError('Applications are still loading. Try again.')
                task = self.runtime.asyncio.create_task(
                    self._send_desktop_result(config.get('id'), command))
                self.desktop_tasks.add(task)
                task.add_done_callback(self.desktop_tasks.discard)
                return
            result = await self.runtime.desktop_control(command)
            if await self.runtime.auth_valid(self.ws.phonemic_auth):
                await self.ws.send(json.dumps({'type': 'desktop', 'id': config.get('id'), 'result': result}))
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError) else 'Desktop control unavailable'
            await self.ws.send(json.dumps({'type': 'desktop', 'id': config.get('id'), 'error': message}))

    async def _send_desktop_result(self, request_id, command):
        try:
            result = await self.runtime.desktop_control(command)
            if await self.runtime.auth_valid(self.ws.phonemic_auth):
                await self.ws.send(json.dumps({'type': 'desktop', 'id': request_id, 'result': result}))
        except Exception as error:
            if await self.runtime.auth_valid(self.ws.phonemic_auth):
                message = str(error) if isinstance(error, RuntimeError) else 'Desktop windows unavailable'
                await self.ws.send(json.dumps({'type': 'desktop', 'id': request_id,
                                               'error': message}))

    async def _mouse(self, config):
        try:
            await self.runtime.mouse_control(config['mouse'])
            await self.ws.send(json.dumps({'type': 'mouse'}))
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError) else 'Laptop mouse unavailable'
            await self.ws.send(json.dumps({'type': 'mouse', 'error': message}))

    async def _herdr(self, config):
        try:
            command = config['herdr']
            if not isinstance(command, dict):
                raise RuntimeError('Invalid terminal command')
            if command.get('action') not in ('list', 'read'):
                await self.runtime.require_desktop(config.get('window'), True)
            result = await self.herdr.control(command)
            await self.ws.send(json.dumps({'type': 'herdr', 'id': config.get('id'), 'result': result}))
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError) else 'Herdr is unavailable on the laptop'
            await self.ws.send(json.dumps({'type': 'herdr', 'id': config.get('id'), 'error': message}))

    async def _dictation(self, config, playback_until):
        try:
            action = config['dictation']
            started_at = self.runtime.time.monotonic()
            print(f"dictation {action if action in ('start', 'stop', 'abort') else 'invalid'} requested", flush=True)
            if action == 'start':
                async with self.runtime.asyncio.timeout(8):
                    await self.dictation.start()
            elif action in ('stop', 'abort'):
                if action == 'stop':
                    await self.runtime.asyncio.sleep(max(0, playback_until - self.runtime.time.monotonic()))
                await self.dictation.finish(abort=action == 'abort')
            else:
                raise RuntimeError('Unknown dictation action')
            print(f"dictation {action} ready after {self.runtime.time.monotonic() - started_at:.2f}s", flush=True)
            await self.ws.send(json.dumps({'type': 'dictation', 'action': action, 'id': config.get('id')}))
        except Exception as error:
            print(f"dictation request failed ({type(error).__name__})", flush=True)
            message = str(error) if isinstance(error, RuntimeError) else \
                'Laptop dictation unavailable; check that its updated daemon is running'
            await self.ws.send(json.dumps({'type': 'dictation', 'error': message, 'id': config.get('id')}))

    async def close(self):
        for task in list(self.desktop_tasks):
            task.cancel()
        if self.desktop_tasks:
            await self.runtime.asyncio.gather(*self.desktop_tasks, return_exceptions=True)
