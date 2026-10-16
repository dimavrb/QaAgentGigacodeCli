"""Local contract/process checks; no live model evaluation."""
import os
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import process_control


class ProcessIdentityTests(unittest.TestCase):
    def test_current_identity_and_reused_pid_protection(self):
        me=process_control.identity(os.getpid())
        self.assertTrue(process_control.matches(me))
        wrong=dict(me,birth='different')
        self.assertFalse(process_control.matches(wrong))
        with self.assertRaisesRegex(ValueError,'unowned'):
            process_control.stop_group(me)
