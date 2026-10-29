"""本机投递日志：提交前落盘，未知提交只回读，绝不再次按 Enter。"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

class DeliveryJournal:
    def __init__(self, path):
        self.path=Path(path);self.lock=threading.RLock();self.rows={}
        if self.path.exists():
            # 损坏时拒绝启动，不把未知投递当成全新任务。
            self.rows=json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.rows,dict): raise ValueError("投递日志格式错误")
    @staticmethod
    def key(group,text,task_id=""):
        return hashlib.sha256((str(group)+"\0"+str(task_id)+"\0"+str(text)).encode()).hexdigest()
    def get(self,key):
        with self.lock: return dict(self.rows.get(key,{"state":"new"}))
    def set(self,key,**values):
        with self.lock:
            row=dict(self.rows.get(key,{}));row.update(values);row["updatedAt"]=time.time()
            # 仅轮转七天前已确认的技术回执；未知提交始终保留。
            updated={k:v for k,v in self.rows.items() if v.get("state")!="confirmed" or time.time()-v.get("updatedAt",time.time())<7*86400}
            if key not in updated and len(updated)>=20000:
                raise RuntimeError("投递回执达到上限，请先处理未知任务")
            updated[key]=row
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temp=self.path.with_suffix(".tmp")
            with temp.open("w",encoding="utf-8") as f:
                json.dump(updated,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
            os.replace(temp,self.path);self.rows=updated
