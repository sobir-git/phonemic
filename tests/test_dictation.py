"""Exercise the phone bridge against an isolated local control socket."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('webmic', Path(__file__).parents[1] / 'lib/webmic.py')
webmic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(webmic)


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
