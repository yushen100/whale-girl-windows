"""按群严格有序、群间公平调度的有界工作队列。"""
from collections import deque
import queue,threading
class ChatExecutor:
    def __init__(self,workers=3,capacity=48,per_chat=8):
        self.capacity=capacity;self.per_chat=per_chat;self.pending={};self.ready=queue.Queue();self.lock=threading.Lock();self.count=0
        for index in range(workers):threading.Thread(target=self._worker,name="whale-chat-"+str(index),daemon=True).start()
    def submit(self,key,fn,*args):
        with self.lock:
            if self.count>=self.capacity or len(self.pending.get(key,[]))>=self.per_chat:return False
            first=key not in self.pending
            self.pending.setdefault(key,deque()).append((fn,args));self.count+=1
            if first:self.ready.put(key)
            return True
    def _worker(self):
        while True:
            key=self.ready.get()
            try:
                with self.lock:fn,args=self.pending[key][0]
                try:fn(*args)
                except Exception:pass
                finally:
                    with self.lock:
                        self.pending[key].popleft();self.count-=1
                        if self.pending[key]:self.ready.put(key)
                        else:del self.pending[key]
            finally:self.ready.task_done()
