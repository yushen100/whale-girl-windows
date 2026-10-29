"""独立微信桥接守护：原生崩溃后自动拉起，人工暂停时保持停止。"""
import argparse
import ctypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import sys


sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"examples"))
from private_log import append as append_private_log


def signed_exit(code):
    return code - 2**32 if code is not None and code >= 2**31 else code


def restart_delay(code, failures):
    return 60 if code in (1, 2) or failures >= 5 else 3


def wait_existing(pid, paused):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return None
        raise OSError(error, "无法接管现有桥接进程")
    try:
        while not paused():
            result = kernel.WaitForSingleObject(handle, 1000)
            if result == 0:
                code = ctypes.c_uint32()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                    raise ctypes.WinError(ctypes.get_last_error())
                return signed_exit(code.value)
            if result != 258:
                raise ctypes.WinError(ctypes.get_last_error())
        return None
    finally:
        kernel.CloseHandle(handle)


def supervise(root, command, watch_pid=0, wait_adopt=wait_existing, stop_after=None,
              sleep=time.sleep, clock=time.monotonic):
    root = Path(root)
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    paused = lambda: (data / "PAUSED").exists()
    log_path = data / "1027_微信桥接守护_v1.log"
    def record(event, **fields):
        row = {"time": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"), "event": event, **fields}
        append_private_log(log_path,json.dumps(row,ensure_ascii=False))
    failures = 0
    completed = 0
    record("守护启动", pid=os.getpid(), adoptedPid=watch_pid)
    while not paused():
        started = clock()
        code = None
        if watch_pid:
            record("接管已有桥接", pid=watch_pid)
            code = wait_adopt(watch_pid, paused)
            watch_pid = 0
        else:
            # 不抢占已经由其他正规入口启动的桥接。
            if stop_after is None:
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", 47821)) == 0:
                        sleep(1)
                        continue
            with (data / "1027_微信桥接后台输出_v1.log").open("ab", buffering=0) as out, \
                 (data / "1027_微信桥接后台错误_v1.log").open("ab", buffering=0) as err:
                env = dict(os.environ, PYTHONIOENCODING="utf-8")
                env.pop("TZ", None)  # Windows 使用系统时区，避免 C 运行库误解析 IANA 时区。
                try:
                    process = subprocess.Popen(command, cwd=root, env=env, stdout=out, stderr=err,
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except OSError as exc:
                    record("启动失败", error=type(exc).__name__)
                    sleep(5)
                    continue
                record("启动桥接", pid=process.pid)
                while process.poll() is None and not paused():
                    sleep(0.5)
                if paused():
                    record("检测到人工暂停", pid=process.pid)
                    return
                code = signed_exit(process.returncode)
        if paused():
            break
        completed += 1
        failures = 1 if clock() - started >= 300 else failures + 1
        delay = restart_delay(code, failures)
        record("桥接退出", exitCode=code, consecutiveFailures=failures, retrySeconds=delay)
        if stop_after is not None and completed >= stop_after:
            return
        deadline = clock() + delay
        while not paused() and clock() < deadline:
            sleep(min(0.5, max(0, deadline - clock())))
    record("人工暂停，守护退出")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch-pid", type=int, default=0)
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 47822))
        lock.listen(1)
    except OSError:
        return
    try:
        supervise(root, [args.python, "-u", str(root / "examples/wechatauto_bridge.py")], args.watch_pid)
    finally:
        lock.close()


if __name__ == "__main__":
    main()
