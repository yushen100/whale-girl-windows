"""微信定位失败恢复：隐藏运行 PowerShell，限频且禁止并发恢复。"""
import json
import os
from pathlib import Path
import subprocess
import threading
import time


class WindowRecovery:
    def __init__(self, root, log, cooldown=120, runner=subprocess.run, clock=time.monotonic):
        self.script = Path(root) / "scripts" / "1027_恢复微信窗口_v1.ps1"
        self.log = log
        self.cooldown = cooldown
        self.runner = runner
        self.clock = clock
        self.last = float("-inf")
        self.lock = threading.Lock()

    def recover(self, reason, hwnd=0, blocked=False):
        if not hwnd:
            self.log("未确认机器人主窗，跳过恢复，不操作其他微信窗口")
            return False
        if blocked or not self.lock.acquire(blocking=False):
            return False
        try:
            now = self.clock()
            if now - self.last < self.cooldown:
                return False
            self.last = now
            # 使用系统自带 PowerShell，参数列表直接传递，不拼接 shell 命令。
            powershell = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            command = [str(powershell), "-NoProfile", "-NonInteractive",
                       "-ExecutionPolicy", "Bypass", "-File", str(self.script),
                       "-WindowHandle", str(int(hwnd or 0))]
            self.log("定位失败，尝试恢复微信窗口（不最大化）：", reason)
            result = self.runner(command, capture_output=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0:
                self.log("微信窗口恢复未成功，请检查微信已登录；120 秒后才会再次尝试")
                return False
            value = json.loads(result.stdout.decode("utf-8-sig").strip())
            if not value.get("restored"):
                return False
            self.log("微信主窗已还原，重新建立窗口定位")
            return True
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.log("微信窗口恢复失败或超时，保留原有重试流程")
            return False
        finally:
            self.lock.release()
