"""守护故障注入：只启动临时测试子进程，不启动微信桥接、不发送消息。"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("guard", root / "scripts/1027_微信桥接守护_v1.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
test_root = root / "data/1027_守护故障注入_v1"
test_root.mkdir(exist_ok=True)


class GuardTests(unittest.TestCase):
    def folder(self):
        # 保留测试目录及结果，便于复核，不动业务数据。
        return Path(tempfile.mkdtemp(prefix="case_", dir=test_root))

    def rows(self, folder):
        return [json.loads(x) for x in (folder / "data/1027_微信桥接守护_v1.log").read_text(encoding="utf-8").splitlines()]

    def test_watchdog_exit_restarts_and_preserves_logs(self):
        folder = self.folder()
        child = "from pathlib import Path; import os; p=Path('starts.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('test-output',flush=True); os._exit(7)"
        guard.supervise(folder, [sys.executable, "-c", child], stop_after=2)
        self.assertEqual((folder / "starts.txt").read_text(), "xx")
        rows = self.rows(folder)
        exits = [row for row in rows if row["event"] == "桥接退出"]
        self.assertEqual([row["exitCode"] for row in exits], [7, 7])
        out = (folder / "data/1027_微信桥接后台输出_v1.log").read_text()
        self.assertEqual(out.count("test-output"), 2)

    def test_native_exit_code_of_adopted_process(self):
        folder = self.folder()
        child = subprocess.Popen([sys.executable, "-c",
            "import time,ctypes; time.sleep(0.5); ctypes.windll.kernel32.ExitProcess(0xc0000005)"],
            creationflags=subprocess.CREATE_NO_WINDOW)
        guard.supervise(folder, [sys.executable, "-c", "from pathlib import Path; Path('restarted.txt').write_text('ok')"], watch_pid=child.pid, stop_after=2)
        child.wait(timeout=5)
        exits = [row for row in self.rows(folder) if row["event"] == "桥接退出"]
        self.assertEqual(exits[0]["exitCode"], -1073741819)
        self.assertEqual(exits[0]["retrySeconds"], 3)
        self.assertEqual((folder / "restarted.txt").read_text(), "ok")

    def test_manual_pause_never_starts_child(self):
        folder = self.folder()
        (folder / "data").mkdir()
        (folder / "data/PAUSED").write_text("test")
        guard.supervise(folder, ["does-not-exist.exe"], stop_after=1)
        self.assertFalse(any(row["event"] == "启动桥接" for row in self.rows(folder)))

    def test_throttle_continuous_and_configuration_errors(self):
        self.assertEqual(guard.restart_delay(7, 1), 3)
        self.assertEqual(guard.restart_delay(-1073741819, 5), 60)
        self.assertEqual(guard.restart_delay(1, 1), 60)
        self.assertEqual(guard.restart_delay(2, 1), 60)


if __name__ == "__main__":
    unittest.main()
