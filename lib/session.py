"""WebSocket lifecycle and the real-time microphone session."""

import json
import subprocess


def _run(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return ''


def listeners(runtime):
    """Return applications currently recording from the virtual microphone."""
    index = None
    for line in _run('pactl', 'list', 'sources', 'short').splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == runtime.SRC:
            index = fields[0]
            break
    if index is None:
        return None
    apps, source = [], None
    for line in _run('pactl', 'list', 'source-outputs').splitlines():
        text = line.strip()
        if text.startswith('Source Output #'):
            source = None
        elif text.startswith('Source:'):
            source = text.split()[1]
        elif text.startswith('application.name') and '=' in text and source == index:
            name = text.split('=', 1)[1].strip().strip('"')
            if name not in apps:
                apps.append(name)
    return apps


async def guard_session(runtime, ws):
    while True:
        await runtime.asyncio.sleep(1)
        if not await runtime.auth_valid(ws.phonemic_auth):
            await ws.close(4401, 'Browser access revoked or expired')
            return


async def report(runtime, ws, state):
    apps, tick = await runtime.asyncio.to_thread(runtime.listeners), 0
    while True:
        if tick % 8 == 0:
            apps = await runtime.asyncio.to_thread(runtime.listeners)
        tick += 1
        try:
            await ws.send(json.dumps({'src': runtime.SRC, 'mic': None if apps is None else True,
                                      'apps': apps or [], 'rx': state['n']}))
        except Exception:
            return
        await runtime.asyncio.sleep(.25)


async def report_desktop(runtime, ws):
    while True:
        try:
            context = await runtime.desktop_context()
        except Exception:
            context = {}
        try:
            await ws.send(json.dumps({'type': 'desktop-context', 'result': context}))
        except Exception:
            return
        await runtime.asyncio.sleep(.8)


async def report_herdr(runtime, ws, herdr):
    previous = None
    while True:
        try:
            inventory = await herdr.control({'action': 'list'})
            current = json.dumps(inventory, sort_keys=True, separators=(',', ':'))
            if current != previous:
                await ws.send(json.dumps({'type': 'herdr-update', 'result': inventory}))
                previous = current
        except runtime.asyncio.CancelledError:
            raise
        except Exception:
            pass
        await runtime.asyncio.sleep(1)


def stop_proc(process):
    """Close and reap the sink process so it releases its sink-input."""
    if process is None:
        return
    try:
        process.stdin.close()
    except Exception:
        pass
    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass


async def stop_sink(runtime, process):
    await runtime.asyncio.to_thread(runtime.stop_proc, process)


def write_audio(process, data):
    process.stdin.write(data)
    process.stdin.flush()


def spawn_sink(runtime, rate):
    return subprocess.Popen([
        'pacat', '--raw', '--playback', f'--device={runtime.SINK}', '--format=s16le',
        f'--rate={rate}', '--channels=1', f'--latency-msec={runtime.LATENCY}',
        '--property=application.name=phonemic-web'], stdin=subprocess.PIPE)


async def handler(runtime, ws):
    credentials = getattr(ws, 'phonemic_auth', (None, None))
    if not await runtime.auth_valid(credentials):
        await ws.close(4401, 'Pair this browser')
        return
    if len(runtime.ACTIVE_CONNECTIONS) >= 4:
        await ws.close(1013, 'Too many connections')
        return
    runtime.ACTIVE_CONNECTIONS.add(ws)
    try:
        await handle_authenticated(runtime, ws)
    finally:
        runtime.ACTIVE_CONNECTIONS.discard(ws)


async def handle_authenticated(runtime, ws):
    try:
        sock = ws.transport.get_extra_info('socket')
        if sock:
            import socket
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    print('paired browser connected', flush=True)
    sink = runtime.spawn_sink(runtime.RATE)
    audio = runtime.AudioSession(runtime.RATE, decoder_factory=runtime.OpusDecoder,
                                 opus_available=lambda: runtime.opus_library() is not None)
    dictation = runtime.Dictation(runtime)
    herdr = runtime.Herdr()
    state, playback_until = {'n': 0}, 0.0
    task = runtime.asyncio.create_task(runtime.report(ws, state))
    herdr_task = runtime.asyncio.create_task(runtime.report_herdr(ws, herdr))
    desktop_task = runtime.asyncio.create_task(runtime.report_desktop(ws))
    guard = runtime.asyncio.create_task(runtime.guard_session(ws))
    worker = runtime.ControlWorker(runtime, ws, herdr, dictation)
    control_task = runtime.asyncio.create_task(worker.run())
    controls, audio_budget = runtime.Budget(40, 80), runtime.Budget(192000, 2 * 1024 * 1024)
    pointer_budget = runtime.Budget(120, 240)
    started = runtime.time.time()
    try:
        async for message in ws:
            if isinstance(message, str):
                if not await runtime.auth_valid(ws.phonemic_auth):
                    await ws.close(4401, 'Browser access revoked or expired')
                    break
                if len(message) > 100000:
                    await ws.close(1008, 'Control message limit exceeded')
                    break
                try:
                    config = json.loads(message)
                except Exception:
                    continue
                if not isinstance(config, dict):
                    continue
                budget = pointer_budget if 'mouse' in config else controls
                if not budget.take():
                    kind = ('mouse' if 'mouse' in config else 'desktop' if 'desktop' in config
                            else 'herdr' if 'herdr' in config else 'dictation')
                    await ws.send(json.dumps({'type': kind, 'id': config.get('id'),
                                              'error': 'Input is arriving too quickly. Try again.'}))
                    continue
                if config.get('audio_v') in (2, 3) or 'audio_codec' in config:
                    response, changed = audio.negotiate(config)
                    if changed:
                        await runtime.stop_sink(sink)
                        sink = runtime.spawn_sink(audio.rate)
                        print(f'audio -> {audio.codec}/{audio.framing} ({audio.rate} Hz)', flush=True)
                    await ws.send(json.dumps(response))
                    continue
                if 'audio_start' in config:
                    await ws.send(json.dumps(audio.start_stream(config['audio_start'])))
                    continue
                if 'audio_end' in config:
                    await ws.send(json.dumps(audio.end_stream(config['audio_end'])))
                    continue
                kind = next((key for key in ('desktop', 'mouse', 'herdr', 'dictation') if key in config), None)
                if kind:
                    if not worker.enqueue(config, playback_until):
                        await ws.send(json.dumps({'type': kind, 'id': config.get('id'),
                                                  'error': 'Input is arriving too quickly. Try again.'}))
                    continue
                rate = config.get('rate', audio.rate)
                if type(rate) is not int or not 8000 <= rate <= 48000:
                    await ws.close(1008, 'Invalid sample rate')
                    break
                if rate != audio.rate:
                    audio.rate = rate
                    await runtime.stop_sink(sink)
                    sink = runtime.spawn_sink(audio.rate)
                    print(f'rate -> {audio.rate} Hz', flush=True)
                continue
            if isinstance(message, bytes) and sink.stdin:
                try:
                    pcm, samples = audio.decode(message, audio_budget)
                except runtime.AudioProtocolError as error:
                    await ws.close(1008, str(error))
                    break
                playback_until = max(runtime.time.monotonic(), playback_until) + samples / audio.rate
                await runtime.asyncio.to_thread(runtime.write_audio, sink, pcm)
                state['n'] += len(pcm)
    except Exception as error:
        print(f'stream ended ({type(error).__name__})', flush=True)
    finally:
        await worker.queue.join()
        for task_to_cancel in (task, herdr_task, desktop_task, control_task, guard):
            task_to_cancel.cancel()
        await runtime.asyncio.gather(task, herdr_task, desktop_task, control_task, guard,
                                     return_exceptions=True)
        await worker.close()
        await dictation.close()
        audio.close()
        await runtime.stop_sink(sink)
        print(f'connection closed (code={getattr(ws, "close_code", None)})', flush=True)
        print(f'phone disconnected after {runtime.time.time() - started:.0f}s '
              f'({state["n"] / 2 / audio.rate:.1f}s of audio)', flush=True)
