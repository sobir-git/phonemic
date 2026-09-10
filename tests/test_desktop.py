import unittest
from unittest.mock import patch, AsyncMock
from lib import webmic

class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_or_closed_window_never_launches_focus(self):
        with patch.object(webmic,'desktop_snapshot',AsyncMock(return_value={'windows':[]})), patch.object(webmic.asyncio,'create_subprocess_exec',AsyncMock()) as spawn:
            for command in ({'action':'focus','window':'--shell'}, {'action':'focus','window':'123'}, {'action':'run','window':'123'}):
                with self.assertRaises(RuntimeError):await webmic.desktop_control(command)
            spawn.assert_not_called()

    async def test_focus_uses_known_id_and_fixed_arguments(self):
        process=AsyncMock();process.returncode=0
        with patch.object(webmic,'desktop_snapshot',AsyncMock(return_value={'windows':[{'id':'123','active':True,'app':'Test'}]})), patch.object(webmic.asyncio,'create_subprocess_exec',AsyncMock(return_value=process)) as spawn:
            result=await webmic.desktop_control({'action':'focus','window':'123'})
            self.assertEqual(result['focused'],'123')
            self.assertEqual(spawn.call_args.args,('xdotool','windowactivate','--sync','123'))

    async def test_list_reuses_short_cache(self):
        with patch.object(webmic,'DESKTOP_CACHE',None), patch.object(webmic,'desktop_snapshot',AsyncMock(return_value={'windows':[]})) as snapshot:
            await webmic.desktop_control({'action':'list'})
            await webmic.desktop_control({'action':'list'})
            snapshot.assert_awaited_once()

    async def test_list_is_metadata_first_and_preview_is_targeted(self):
        metadata={'windows':[{'id':'123','active':True,'app':'Test'}],'previewSupported':True}
        async def snapshot(*, previews=False, context=False, window_id=None):
            if window_id is not None:
                return {'window':window_id,'preview':'encoded-preview'}
            self.assertFalse(previews);self.assertFalse(context)
            return metadata
        with patch.object(webmic,'DESKTOP_CACHE',None), patch.object(webmic,'DESKTOP_UPDATED',0), \
             patch.object(webmic,'DESKTOP_SNAPSHOT',''), patch.object(webmic,'DESKTOP_PREVIEW_CACHE',webmic.OrderedDict()), \
             patch.object(webmic,'desktop_snapshot',side_effect=snapshot) as run:
            listing=await webmic.desktop_control({'action':'list'})
            self.assertTrue(listing['snapshot'])
            self.assertNotIn('preview', listing['windows'][0])
            result=await webmic.desktop_control({'action':'preview','window':'123','snapshot':listing['snapshot']})
            self.assertEqual(result['preview'],'encoded-preview')
            self.assertEqual(run.await_count,2)
            with self.assertRaises(RuntimeError):
                await webmic.desktop_control({'action':'preview','window':'123','snapshot':'stale'})

    async def test_active_context_uses_targeted_context_lookup(self):
        with patch.object(webmic,'desktop_snapshot',AsyncMock(return_value={'id':'123','app':'Editor','herdr':False,'width':800,'height':600})) as snapshot:
            result=await webmic.desktop_context(fresh=True)
            self.assertEqual(result['id'],'123')
            snapshot.assert_awaited_once_with(context=True)

    async def test_input_routes_to_generic_app_and_not_herdr(self):
        herdr=AsyncMock()
        with patch.object(webmic,'require_desktop',AsyncMock(return_value={'id':'123','herdr':False})), patch.object(webmic,'generic_input',AsyncMock(return_value={})) as generic:
            command={'action':'text','text':'Hello'}
            await webmic.route_desktop_input({'window':'123','herdr':False,'command':command},herdr)
            generic.assert_awaited_once_with(command,'123');herdr.control.assert_not_called()

    async def test_input_routes_to_herdr_only_for_matching_context(self):
        herdr=AsyncMock()
        with patch.object(webmic,'require_desktop',AsyncMock(return_value={'id':'123','herdr':True})), patch.object(webmic,'generic_input',AsyncMock()) as generic:
            command={'action':'key','key':'enter'}
            await webmic.route_desktop_input({'window':'123','herdr':True,'command':command},herdr)
            herdr.control.assert_awaited_once_with(command);generic.assert_not_called()

    async def test_stale_window_and_mode_reject_input(self):
        with patch.object(webmic,'desktop_context',AsyncMock(return_value={'id':'456','herdr':False})):
            for wid,mode in [('123',False),('456',True)]:
                with self.assertRaises(RuntimeError):await webmic.require_desktop(wid,mode)

    async def test_text_uses_stdin_not_shell_or_command_arguments(self):
        proc=AsyncMock();proc.returncode=0
        with patch.object(webmic,'require_desktop',AsyncMock()), patch.object(webmic.asyncio,'create_subprocess_exec',AsyncMock(return_value=proc)) as spawn:
            await webmic.generic_input({'action':'text','text':'literal $(test)'},'123')
            self.assertNotIn('literal $(test)',spawn.call_args.args)
            proc.communicate.assert_awaited_once_with(b'literal $(test)')
