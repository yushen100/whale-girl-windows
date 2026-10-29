"""只测试恢复调度，不操作微信、不发送消息。"""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "wechat_recovery", Path(__file__).resolve().parents[1] / "examples/wechat_window_recovery.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RecoveryTests(unittest.TestCase):
    def make_recovery(self, runner=None):
        self.calls = []
        self.now = 0
        def fake(command, **kwargs):
            self.calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout=b'{"restored":true}')
        return module.WindowRecovery(Path(__file__).resolve().parents[1],
                                     lambda *args: None, runner=runner or fake,
                                     clock=lambda: self.now)

    def test_unknown_target_never_runs_window_action(self):
        recovery=self.make_recovery()
        self.assertFalse(recovery.recover("UIA",0))
        self.assertEqual(self.calls,[])

    def test_success_and_cooldown(self):
        recovery = self.make_recovery()
        self.assertTrue(recovery.recover("UIA", 123))
        self.assertFalse(recovery.recover("OCR", 123))
        self.now = 120
        self.assertTrue(recovery.recover("UIA", 123))
        self.assertEqual(len(self.calls), 2)
        command, options = self.calls[0]
        self.assertEqual(command[-2:], ["-WindowHandle", "123"])
        self.assertIn("-NonInteractive", command)
        self.assertTrue(options["capture_output"])
        self.assertEqual(options["timeout"], 15)
        self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertNotIn("shell", options)

    def test_fullscreen_and_concurrent_recovery_do_nothing(self):
        recovery = self.make_recovery()
        self.assertFalse(recovery.recover("UIA", blocked=True))
        recovery.lock.acquire()
        try:
            self.assertFalse(recovery.recover("UIA",123))
        finally:
            recovery.lock.release()
        self.assertEqual(self.calls, [])
        self.assertTrue(recovery.recover("UIA",123))

    def test_failure_is_also_throttled(self):
        calls = []
        def failure(*args, **kwargs):
            calls.append(True)
            return SimpleNamespace(returncode=1, stdout=b"")
        recovery = self.make_recovery(failure)
        self.assertFalse(recovery.recover("UIA",123))
        self.now = 119
        self.assertFalse(recovery.recover("UIA",123))
        self.assertEqual(len(calls), 1)

    def test_timeout_and_bad_output_do_not_break_bridge(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("powershell", 15)
        self.assertFalse(self.make_recovery(timeout).recover("UIA",123))
        def invalid(*args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=b"invalid json")
        self.assertFalse(self.make_recovery(invalid).recover("OCR",123))


if __name__ == "__main__":
    unittest.main()
