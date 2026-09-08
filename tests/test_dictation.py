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


class BufferedAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_waits_for_buffered_speech_to_play(self):
        samples = b'\x01\x00' * (webmic.RATE // 2)
        class Phone:
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
             patch.object(webmic, 'time', clock), \
             patch.object(webmic.asyncio, 'sleep', new=AsyncMock()) as sleep:
            await webmic.handler(Phone())
        sleep.assert_awaited_once_with(0.5)
        sink.stdin.write.assert_called_once_with(samples)
        bridge.finish.assert_awaited_once_with(abort=False)


class HerdrTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_exposes_only_picker_metadata(self):
        remote = webmic.Herdr()
        remote.request = AsyncMock(side_effect=[
            {'workspaces': [{'workspace_id': 'w1', 'label': 'Project'}]},
            {'panes': [{'workspace_id': 'w1', 'pane_id': 'w1:p1',
                        'terminal_title_stripped': 'Agent', 'focused': True, 'agent': 'codex', 'agent_status': 'working',
                        'agent_session': {'private': 'not needed'}}]},
        ])
        result = await remote.control({'action': 'list'})
        self.assertEqual(result, {'panes': [{'id': 'w1:p1', 'workspace': 'Project',
                                           'title': 'Agent', 'focused': True, 'workspace_id': 'w1',
                                           'workspace_state': 'unknown', 'state': 'working', 'agent': 'codex'}]})

    async def test_modifiers_and_keys_target_explicit_pane(self):
        remote = webmic.Herdr()
        remote.request = AsyncMock(return_value={})
        await remote.control({'action': 'key', 'pane': 'w2:p3', 'key': 'backspace',
                              'modifiers': ['alt', 'ctrl']})
        remote.request.assert_awaited_once_with('pane.send_keys', {
            'pane_id': 'w2:p3', 'keys': ['ctrl+alt+backspace']})

    async def test_scroll_is_clamped_to_available_history(self):
        remote = webmic.Herdr()
        remote.request = AsyncMock(side_effect=[
            {'pane': {'scroll': {'offset_from_bottom': 95, 'max_offset_from_bottom': 100,
                                 'viewport_rows': 40}}}, {},
        ])
        await remote.control({'action': 'scroll', 'pane': 'w2:p3', 'direction': 'up'})
        self.assertEqual(remote.request.await_args.args, ('pane.scroll', {
            'pane_id': 'w2:p3', 'offset_from_bottom': 100}))

    async def test_unlisted_keys_and_arbitrary_methods_never_reach_herdr(self):
        remote = webmic.Herdr()
        remote.request = AsyncMock()
        for command in (
            {'action': 'server.stop'},
            {'action': 'key', 'key': 'some text'},
            {'action': 'key', 'key': 'enter', 'modifiers': ['super']},
            {'action': 'key', 'key': 'c'},
            {'action': 'scroll', 'direction': 'sideways'},
        ):
            with self.assertRaises(RuntimeError):
                await remote.control({'pane': 'w2:p3', **command})
        remote.request.assert_not_awaited()
