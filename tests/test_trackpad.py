"""Mouse commands must stay bounded and never become shell input."""
import unittest
from unittest.mock import AsyncMock, patch
from test_dictation import webmic


class TrackpadTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands(self):
        proc = AsyncMock(returncode=0)
        with patch.object(webmic, 'TOKEN', 'test'), patch.dict(webmic.os.environ, {'DISPLAY': ':0', 'XDG_SESSION_TYPE': 'x11'}), patch.object(webmic.asyncio, 'create_subprocess_exec', return_value=proc) as spawn:
            await webmic.mouse_control({'action': 'move', 'dx': -25, 'dy': 10})
            self.assertEqual(spawn.call_args.args, ('xdotool', 'mousemove_relative', '--', '-25', '10'))
            for button, number in [('left', '1'), ('right', '3')]:
                await webmic.mouse_control({'action': 'click', 'button': button})
                self.assertEqual(spawn.call_args.args, ('xdotool', 'click', number))

    async def test_reject_invalid_commands(self):
        with patch.object(webmic, 'TOKEN', 'test'), patch.object(webmic.asyncio, 'create_subprocess_exec') as spawn:
            for cmd in [None, {}, {'action': 'click', 'button': '4'},
                        {'action': 'move', 'dx': True, 'dy': 0},
                        {'action': 'move', 'dx': 501, 'dy': 0},
                        {'action': 'move', 'dx': '1; touch /tmp/no', 'dy': 0}]:
                with self.assertRaises(RuntimeError):
                    await webmic.mouse_control(cmd)
            spawn.assert_not_called()

    async def test_require_token(self):
        with patch.object(webmic, 'TOKEN', ''), self.assertRaisesRegex(RuntimeError, 'token'):
            await webmic.mouse_control({'action': 'click', 'button': 'left'})

    async def test_wayland_error(self):
        with patch.object(webmic, 'TOKEN', 'test'), patch.dict(webmic.os.environ, {'XDG_SESSION_TYPE': 'wayland'}), self.assertRaisesRegex(RuntimeError, 'X11'):
            await webmic.mouse_control({'action': 'click', 'button': 'left'})
