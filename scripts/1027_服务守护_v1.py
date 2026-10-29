"""独立鲸鲸服务守护：node server.js 意外退出后自动拉起，人工暂停时保持停止。

为什么需要它（2026-10-27 事故）：
  23:46:09 鲸鲸服务（node server.js，127.0.0.1:8787）在处理群消息时整个进程消失。
  当时没有任何守护 —— 服务死了就是死了，桥接每 30~60 秒撞一次 8787 被拒绝，
  在群里 @ 主人三次，报的还是「模型调用失败」。真相是后台没了，模型一直是好的。
  主人 23:50 说「没事我修修看」，23:54 手动拉起后才恢复。哑了 8 分钟。

与桥接守护（1027_微信桥接守护_v1.py）的关系：
  两个守护互不相干，各自管各自的进程。桥接守护看的是桥接进程，这里看的是 node。
  共用的只有 data/PAUSED 暂停标记 —— 「全部停止」时两边一起收手，
  避免主人点了停止、守护又把进程拉起来。

为什么不在这里做健康检查（比如定期打 8787）：
  端口还听着但服务卡死的场景确实存在，但那种情况重启会丢掉正在处理的对话，
  得不偿失。这里只负责「进程没了就拉起来」这一件事，卡死交给人工判断。
"""
import argparse
import ctypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"examples"))
from private_log import append as append_private_log


def signed_exit(code):
    """把 Windows 的无符号退出码还原成有符号值（崩溃常见 -1073741819）。"""
    return code - 2**32 if code is not None and code >= 2**31 else code


def restart_delay(code, failures):
    """决定等多久再拉起。

    这里跟桥接守护（1027_微信桥接守护_v1.py）的取舍**故意不同**。

    桥接把退出码 1/2 当成「配置错了，重启也没用」，等 60 秒。
    但服务（server.js）的退出码 1 大多不是配置错 —— 是**被外部强杀**：
    任务管理器、系统回收、内存压力、别的脚本误杀，都算 1。
    真配置错的情况反而很少走到这里：端口被占时下面的 probe 会发现 8787
    有人在听，直接跳过不起进程。

    代价不对等：
      · 等太久的代价 —— 她哑一分钟，群里开始刷「鱼呢」，桥接 @ 主人报警；
      · 重启太快的代价 —— 最坏是崩溃循环，但那由 failures 兜住。

    所以这里一律 3 秒快拉；连续失败 5 次才退化成 60 秒（那时说明不是偶发，
    多半真出事了，让日志安静下来等人工介入）。
    """
    return 60 if failures >= 5 else 3


def wait_existing(pid, paused):
    """接管一个已经在跑的进程，等它退出（不重启，只观察）。"""
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
        if error == 87:          # 进程已经没了
            return None
        raise OSError(error, "无法接管现有服务进程")
    try:
        while not paused():
            result = kernel.WaitForSingleObject(handle, 1000)
            if result == 0:
                code = ctypes.c_uint32()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                    raise ctypes.WinError(ctypes.get_last_error())
                return signed_exit(code.value)
            if result != 258:    # 258 = WAIT_TIMEOUT，继续等
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
    log_path = data / "1027_服务守护_v1.log"

    def record(event, **fields):
        row = {
            "time": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        append_private_log(log_path,json.dumps(row,ensure_ascii=False))

    failures = 0
    completed = 0
    record("守护启动", pid=os.getpid(), adoptedPid=watch_pid)

    while not paused():
        started = clock()
        code = None
        if watch_pid:
            record("接管已有服务", pid=watch_pid)
            code = wait_adopt(watch_pid, paused)
            watch_pid = 0
        else:
            # 不抢占别的正规入口（控制台 / 启动鲸鱼娘.bat）已经拉起的服务。
            # 8787 有人在听就说明服务活着，安静等下一轮。
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", 8787)) == 0:
                    sleep(1)
                    continue
            with (data / "1027_鲸鲸后台输出_v1.log").open("ab", buffering=0) as out, \
                 (data / "1027_鲸鲸后台错误_v1.log").open("ab", buffering=0) as err:
                env = dict(os.environ, PYTHONIOENCODING="utf-8")
                env.pop("TZ", None)      # Windows 用系统时区，避免 C 运行库误解析 IANA 时区
                # 代理清理：跟 start.ps1 / whale-console.ps1 保持一致。
                # 代理没开时残留的 HTTPS_PROXY 会让模型请求连到 127.0.0.1:7897 而失败。
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                             "http_proxy", "https_proxy", "all_proxy"):
                    env.pop(name, None)
                env["NO_PROXY"] = "127.0.0.1,localhost,::1"
                env["no_proxy"] = env["NO_PROXY"]
                try:
                    process = subprocess.Popen(
                        command, cwd=root, env=env, stdout=out, stderr=err,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except OSError as exc:
                    record("启动失败", error=type(exc).__name__)
                    sleep(5)
                    continue
                record("启动服务", pid=process.pid)
                while process.poll() is None and not paused():
                    sleep(0.5)
                if paused():
                    record("检测到人工暂停", pid=process.pid)
                    return
                code = signed_exit(process.returncode)

        if paused():
            break
        completed += 1
        # 跑满 5 分钟才算「稳定」，否则累计失败次数，用来拉长重试间隔
        failures = 1 if clock() - started >= 300 else failures + 1
        delay = restart_delay(code, failures)
        record("服务退出", exitCode=code, consecutiveFailures=failures, retrySeconds=delay)
        if stop_after is not None and completed >= stop_after:
            return
        deadline = clock() + delay
        while not paused() and clock() < deadline:
            sleep(min(0.5, max(0, deadline - clock())))

    record("人工暂停，守护退出")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch-pid", type=int, default=0)
    parser.add_argument("--node", required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]

    # 单实例锁：两个守护同时跑会各拉一个 node，8787 抢不到的那个反复崩溃。
    # 用独立端口，跟桥接守护的 47822 分开。
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 47823))
        lock.listen(1)
    except OSError:
        return

    # 诊断参数：原生崩溃时吐报告。
    #
    # 2026-10-28 00:16:42 服务以 0xC0000409（STATUS_STACK_BUFFER_OVERRUN）退出，
    # 这是原生崩溃 —— server.js 里的 uncaughtException 兜底抓不到，什么都留不下。
    # --report-on-fatalerror 是唯一能在这种场景留下 V8 原始报错和 JS 栈的手段。
    # 只在崩溃时生成文件，正常运行零开销。
    reports = root / "data" / "reports"
    try:
        reports.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    node_args = [
        "--report-on-fatalerror",
        "--report-directory=" + str(reports),
        "--report-compact",
        # 不传 --report-filename：默认命名带时间戳+PID+序号，
        # 每次崩溃都留一份。指定固定名字会**覆盖**上一份（实测过），丢现场。
    ]

    command = [args.node] + node_args + [str(root / "server.js"), "--no-browser"]
    try:
        supervise(root, command, args.watch_pid)
    finally:
        lock.close()


if __name__ == "__main__":
    main()
