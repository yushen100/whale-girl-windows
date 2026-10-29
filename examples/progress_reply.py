"""总等待不被进度发送阻塞；最多一个未完成提示，不堆积后台线程。"""
import threading
import time
_PROGRESS_SLOT = threading.Lock()

def wait_for_response(request, progress, delay=6, total=35):
    started = time.monotonic()
    ready = threading.Event()
    cancelled = threading.Event()
    box = {}
    def worker():
        try: box["value"] = request()
        except Exception as exc: box["error"] = exc
        finally: ready.set()
    def notify():
        try:
            if not ready.is_set() and not cancelled.is_set(): progress()
        except Exception: pass
        finally: _PROGRESS_SLOT.release()
    threading.Thread(target=worker, name="whale-local-reply", daemon=True).start()
    try:
        if not ready.wait(min(max(0, delay), max(0, total))):
            if not ready.is_set() and time.monotonic()-started < total and _PROGRESS_SLOT.acquire(False):
                threading.Thread(target=notify, name="whale-progress", daemon=True).start()
            if not ready.wait(max(0, total-(time.monotonic()-started))):
                raise TimeoutError("本轮等待回复超时")
        if "error" in box: raise box["error"]
        return box.get("value")
    finally: cancelled.set()
