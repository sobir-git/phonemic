"""Herdr command contracts, independent of the receiver and audio stack."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from lib.herdr import Herdr


class HerdrTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.path = str(Path(self.root.name) / 'herdr.sock')
        self.requests = []
        self.received = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.response = b'{"result":{"panes":[]}}\n'
        self.server = await asyncio.start_unix_server(self.serve, self.path)
        self.environment = patch.dict(os.environ, {'PM_HERDR_SOCKET': self.path})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def serve(self, reader, writer):
        try:
            self.requests.append(json.loads(await reader.readline()))
            self.received.set()
            if self.response is not None:
                writer.write(self.response)
                await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            self.disconnected.set()

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        await asyncio.wait_for(self.disconnected.wait(), 1)

    async def test_request_sends_protocol_envelope_and_returns_result(self):
        result = await Herdr().request('pane.list', {})
        self.assertEqual(result, {'panes': []})
        self.assertEqual(self.requests, [
            {'id': 'phonemic', 'method': 'pane.list', 'params': {}},
        ])

    async def test_remote_error_is_preserved_and_connection_closed(self):
        self.response = b'{"error":{"message":"Pane is gone"}}\n'
        with self.assertRaisesRegex(RuntimeError, 'Pane is gone'):
            await Herdr().request('pane.get', {'pane_id': 'w1:p1'})

    async def test_malformed_response_closes_connection(self):
        self.response = b'not JSON\n'
        with self.assertRaises(json.JSONDecodeError):
            await Herdr().request('pane.list', {})

    async def test_cancellation_closes_connection(self):
        self.response = None
        task = asyncio.create_task(Herdr().request('pane.list', {}))
        try:
            await asyncio.wait_for(self.received.wait(), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class HerdrTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_scroll_sends_mouse_wheel_to_selected_pane(self):
        for direction, button in [('up', 64), ('down', 65)]:
            remote = Herdr()
            remote.request = AsyncMock(side_effect=[
                {'pane': {'agent': 'claude', 'scroll': {'max_offset_from_bottom': 0}}},
                {'layout': {'panes': [{'pane_id': 'w1:p1', 'rect': {'width': 110, 'height': 34}}]}}, {}])
            await remote.control({'action': 'scroll', 'pane': 'w1:p1', 'direction': direction})
            self.assertEqual(remote.request.await_args.args, ('pane.send_text',
                {'pane_id': 'w1:p1', 'text': f'\x1b[<{button};55;17M'*3}))

    async def test_claude_latest_wheels_until_screen_stops_changing(self):
        remote = Herdr()
        remote.request = AsyncMock(side_effect=[
            {'pane': {'agent': 'claude', 'scroll': {'max_offset_from_bottom': 0}}},
            {'layout': {'panes': [{'pane_id': 'w1:p1', 'rect': {'width': 110, 'height': 34}}]}},
            {'read': {'text': 'older'}}, {}, {'read': {'text': 'latest'}}, {}, {'read': {'text': 'latest'}}])
        await remote.control({'action': 'scroll', 'pane': 'w1:p1', 'direction': 'bottom'})
        wheels = [c.args for c in remote.request.await_args_list if c.args[0] == 'pane.send_text']
        self.assertEqual(wheels, [('pane.send_text', {'pane_id': 'w1:p1', 'text': '\x1b[<65;55;17M'*40})]*2)

    async def test_commands_and_clear_input(self):
        remote = Herdr()
        remote.request = AsyncMock(return_value={})
        await remote.control({'action': 'command', 'pane': 'w1:p1', 'text': 'cc-yolo'})
        self.assertEqual([c.args for c in remote.request.await_args_list], [
            ('pane.send_text', {'pane_id': 'w1:p1', 'text': 'cc-yolo'})])
        remote.request.reset_mock()
        for text in ['', 'a\nb', '\x1b', 'a'*2001]:
            with self.assertRaises(RuntimeError):
                await remote.control({'action': 'command', 'pane': 'w1:p1', 'text': text})
        remote.request.assert_not_awaited()
        await remote.control({'action': 'key', 'pane': 'w1:p1', 'key': 'u', 'modifiers': ['ctrl']})
        remote.request.assert_awaited_once_with('pane.send_keys', {'pane_id': 'w1:p1', 'keys': ['ctrl+u']})

    async def test_composer_inserts_multiline_text_without_keys(self):
        remote = Herdr()
        remote.request = AsyncMock(return_value={})
        await remote.control({'action': 'text', 'pane': 'w1:p1', 'text': 'hello\nworld'})
        remote.request.assert_awaited_once_with('pane.send_text', {'pane_id': 'w1:p1', 'text': 'hello\nworld'})
        remote.request.reset_mock()
        for text in ['\x1b[31m', 'a'*20001]:
            with self.assertRaises(RuntimeError):
                await remote.control({'action': 'text', 'pane': 'w1:p1', 'text': text})
        remote.request.assert_not_awaited()

    async def test_create_workspace_uses_selected_directory(self):
        remote = Herdr()
        remote.request = AsyncMock(side_effect=[{'pane': {'cwd': '/tmp/project'}}, {'root_pane': {'pane_id': 'w2:p1'}}])
        result = await remote.control({'action': 'workspace', 'pane': 'w1:p1', 'label': 'New project'})
        self.assertEqual(result, {'pane': 'w2:p1'})
        self.assertEqual(remote.request.await_args.args, ('workspace.create', {'label': 'New project', 'cwd': '/tmp/project', 'focus': True}))
        remote.request.reset_mock()
        with self.assertRaises(RuntimeError):
            await remote.control({'action': 'workspace', 'label': 'New', 'cwd': 'relative/path'})
        remote.request.assert_not_awaited()

    async def test_split_preserves_space_and_directory(self):
        remote = Herdr()
        remote.request = AsyncMock(side_effect=[{'pane': {'workspace_id': 'w1', 'cwd': '/tmp/project'}}, {'pane': {'pane_id': 'w1:p2'}}])
        self.assertEqual(await remote.control({'action': 'split', 'pane': 'w1:p1'}), {'pane': 'w1:p2'})
        self.assertEqual(remote.request.await_args.args, ('pane.split', {
            'target_pane_id': 'w1:p1', 'workspace_id': 'w1', 'direction': 'down', 'cwd': '/tmp/project', 'focus': True}))

    async def test_inventory_exposes_only_picker_metadata(self):
        remote = Herdr()
        remote.request = AsyncMock(side_effect=[
            {'workspaces': [{'workspace_id': 'w1', 'label': 'Project'}]},
            {'panes': [{'workspace_id': 'w1', 'pane_id': 'w1:p1',
                        'terminal_title_stripped': 'Agent', 'focused': True, 'agent': 'codex', 'agent_status': 'working',
                        'agent_session': {'private': 'not needed'}, 'tab_id': 'w1:t1'}]},
            {'tabs': [{'tab_id': 'w1:t1', 'label': 'chatbot'}]},
        ])
        result = await remote.control({'action': 'list'})
        self.assertEqual(result, {'panes': [{'id': 'w1:p1', 'workspace': 'Project',
                                           'title': 'chatbot', 'focused': True, 'workspace_id': 'w1',
                                           'workspace_state': 'unknown', 'state': 'working', 'agent': 'codex'}]})
        self.assertEqual([call.args for call in remote.request.await_args_list], [
            ('workspace.list', {}), ('pane.list', {}), ('tab.list', {})])

    async def test_modifiers_and_keys_target_explicit_pane(self):
        remote = Herdr()
        remote.request = AsyncMock(return_value={})
        await remote.control({'action': 'key', 'pane': 'w2:p3', 'key': 'backspace',
                              'modifiers': ['alt', 'ctrl']})
        remote.request.assert_awaited_once_with('pane.send_keys', {
            'pane_id': 'w2:p3', 'keys': ['ctrl+alt+backspace']})

    async def test_scroll_is_clamped_to_available_history(self):
        remote = Herdr()
        remote.request = AsyncMock(side_effect=[
            {'pane': {'scroll': {'offset_from_bottom': 95, 'max_offset_from_bottom': 100,
                                 'viewport_rows': 40}}}, {},
        ])
        await remote.control({'action': 'scroll', 'pane': 'w2:p3', 'direction': 'up'})
        self.assertEqual(remote.request.await_args.args, ('pane.scroll', {
            'pane_id': 'w2:p3', 'offset_from_bottom': 100}))

    async def test_unlisted_keys_and_arbitrary_methods_never_reach_herdr(self):
        remote = Herdr()
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


class OutputReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_is_bounded_and_targets_requested_pane(self):
        remote = Herdr()
        remote.request = AsyncMock(return_value={'read': {'text': 'a'*120001, 'truncated': False}})
        result = await remote.control({'action': 'read', 'pane': 'w4:p2', 'lines': 999999})
        remote.request.assert_awaited_once_with('pane.read', {
            'pane_id': 'w4:p2', 'source': 'visible', 'format': 'ansi', 'strip_ansi': False, 'lines': 160})
        self.assertEqual(result['pane'], 'w4:p2')
        self.assertEqual(len(result['text']), 120000)
        self.assertTrue(result['truncated'])
