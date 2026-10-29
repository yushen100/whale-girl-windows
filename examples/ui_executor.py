"""所有微信界面动作只在同一个COM线程执行；队列与等待均有界。"""
import queue
import threading

class UiExecutor:
    def __init__(self,capacity=32,initialize=True):
        self.initialize=initialize
        self.queue=queue.Queue(maxsize=capacity)
        self.thread=threading.Thread(target=self._run,name="whale-ui-owner",daemon=True)
        self.thread.start()
    def _run(self):
        try:
            if self.initialize:
                import uiautomation
                uiautomation.InitializeUIAutomationInCurrentThread()
        except Exception: pass
        while True:
            fn,ready,box=self.queue.get()
            try:
                if not box.get("cancelled"):
                    try:box["value"]=fn()
                    except BaseException as error:box["error"]=error
            finally:ready.set();self.queue.task_done()
    def call(self,fn,timeout=43):
        if threading.current_thread() is self.thread:return fn()
        ready=threading.Event();box={}
        try:self.queue.put_nowait((fn,ready,box))
        except queue.Full:raise TimeoutError("微信界面队列已满")
        if not ready.wait(timeout):
            box["cancelled"]=True
            raise TimeoutError("微信界面任务超时，等待守护恢复")
        if "error" in box:raise box["error"]
        return box.get("value")
