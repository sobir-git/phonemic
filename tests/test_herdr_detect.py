import unittest
from unittest.mock import patch,Mock
from lib import herdr_detect

class DetectorTests(unittest.TestCase):
    def test_process_exit_pid_reuse_background_and_other_tab_reject_detection(self):
        record={'pid':123,'start':'100','tab':2}
        live={'pid':123,'start':'100','name':'herdr','foreground':True}
        with patch.object(herdr_detect,'records',return_value=[record]),patch.object(herdr_detect,'active_tab',return_value=2),patch.object(herdr_detect,'process_identity',return_value=live) as identity:
            self.assertTrue(herdr_detect.detected(None,None))
            for replacement in (None,{**live,'start':'101'},{**live,'name':'zsh'},{**live,'foreground':False}):
                identity.return_value=replacement
                self.assertFalse(herdr_detect.detected(None,None))
            identity.return_value=live
            with patch.object(herdr_detect,'active_tab',return_value=1):
                self.assertFalse(herdr_detect.detected(None,None))
