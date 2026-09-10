"""Exercise the phone bridge against an isolated local control socket."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from types import SimpleNamespace

from lib import webmic


class DictationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.path = str(Path(self.root.name) / 'daemon.sock')
        self.commands = []
        self.disconnected = asyncio.Event()
        self.reject = False
        self.server = await asyncio.start_unix_server(self.serve, self.path)
        self.environment = patch.dict(os.environ, {'STT_SOCKET_PATH': self.path})
        self.environment.start()
        self.bridge = webmic.Dictation()

    async def serve(self, reader, writer):
        async def send(message):
            writer.write((json.dumps(message) + '\n').encode())
            await writer.drain()
        try:
            await send({'type': 'state', 'protocol': 2})
            while line := await reader.readline():
                command = json.loads(line)
                self.commands.append(command)
                if command['cmd'] == 'start_recording':
                    if self.reject:
                        await send({'type': 'error', 'message': 'Dictation is paused or busy'})
                    else:
                        await send({'type': 'state', 'recording': True})
                        await send({'type': 'recording_started'})
                        await send({'type': 'audio_level', 'capture_ready': True})
                else:
                    await send({'type': 'recording_stopped'})
        finally:
            writer.close()
            await writer.wait_closed()
            self.disconnected.set()

    async def asyncTearDown(self):
        await self.bridge.close()
        self.server.close()
        await self.server.wait_closed()
        self.environment.stop()
        self.root.cleanup()

    async def test_press_release_selects_phone_and_stops(self):
        await self.bridge.start()
        await self.bridge.finish()
        self.assertEqual(self.commands, [
            {'cmd': 'start_recording', 'pipewire_node': webmic.SRC},
            {'cmd': 'stop_recording'},
        ])
        self.assertIsNone(self.bridge.writer)

    async def test_disconnect_closes_owned_connection_without_submitting(self):
        await self.bridge.start()
        await self.bridge.close()
        await asyncio.wait_for(self.disconnected.wait(), 1)
        self.assertEqual(len(self.commands), 1)

    async def test_busy_error_is_reported_and_connection_closed(self):
        self.reject = True
        with self.assertRaisesRegex(RuntimeError, 'paused or busy'):
            await self.bridge.start()
        self.assertIsNone(self.bridge.writer)

    async def test_cancel_sends_abort(self):
        await self.bridge.start()
        await self.bridge.finish(abort=True)
        self.assertEqual(self.commands[-1], {'cmd': 'abort_recording'})


class BufferedAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_waits_for_buffered_speech_to_play(self):
        samples = b'\x01\x00' * (webmic.RATE // 2)
        class Phone:
            phonemic_auth = ("test", "http://localhost")
            remote_address = None
            transport = None
            send = AsyncMock()
            async def __aiter__(self):
                for message in ('{"dictation":"start"}', samples, '{"dictation":"stop"}'):
                    yield message
        bridge = SimpleNamespace(start=AsyncMock(), finish=AsyncMock(), close=AsyncMock())
        sink = MagicMock()
        clock = SimpleNamespace(monotonic=lambda: 100.0, time=lambda: 100.0)
        with patch.object(webmic, 'Dictation', return_value=bridge), \
             patch.object(webmic, 'spawn_sink', return_value=sink), \
             patch.object(webmic, 'stop_proc'), \
             patch.object(webmic, 'report', new=AsyncMock()), \
             patch.object(webmic, 'report_desktop', new=AsyncMock()), \
             patch.object(webmic, 'report_herdr', new=AsyncMock()), \
             patch.object(webmic, 'require_desktop', new=AsyncMock(return_value={'id':'123','herdr':False})), \
             patch.object(webmic, 'guard_session', new=AsyncMock()), \
             patch.object(webmic.AUTH, 'valid', return_value=True), \
             patch.object(webmic, 'time', clock), \
             patch.object(webmic.asyncio, 'sleep', new=AsyncMock()) as sleep:
            await webmic.handler(Phone())
        sleep.assert_awaited_once_with(0.5)
        sink.stdin.write.assert_called_once_with(samples)
        bridge.finish.assert_awaited_once_with(abort=False)

    async def test_slow_control_does_not_delay_audio(self):
        started = asyncio.Event()
        release = asyncio.Event()
        samples = b"\x01\x00" * 480

        class Remote:
            async def control(self, command):
                started.set()
                await release.wait()
                return {"panes": []}

        class Phone:
            phonemic_auth = ("test", "http://localhost")
            remote_address = None
            transport = None
            send = AsyncMock()

            async def __aiter__(self):
                yield json.dumps({"herdr": {"action": "list"}, "id": 1})
                yield samples

        bridge = SimpleNamespace(start=AsyncMock(), finish=AsyncMock(), close=AsyncMock())
        sink = MagicMock()
        with patch.object(webmic, "Herdr", return_value=Remote()), \
             patch.object(webmic, "Dictation", return_value=bridge), \
             patch.object(webmic, "spawn_sink", return_value=sink), \
             patch.object(webmic, "stop_proc"), \
             patch.object(webmic, "report", new=AsyncMock()), \
             patch.object(webmic, "report_desktop", new=AsyncMock()), \
             patch.object(webmic, "report_herdr", new=AsyncMock()), \
             patch.object(webmic, "guard_session", new=AsyncMock()), \
             patch.object(webmic.AUTH, "valid", return_value=True):
            task = asyncio.create_task(webmic.handler(Phone()))
            await asyncio.wait_for(started.wait(), 1)
            sink.stdin.write.assert_called_once_with(samples)
            release.set()
            await task
