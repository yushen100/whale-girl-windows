"""默认不记录身份与聊天正文，诊断文件有界轮转。"""
import re
import threading
from pathlib import Path
_LOCK=threading.Lock()
def redact(text):
    text=str(text)
    if re.search(r"收到|回复.*[←→：:]|图片识别[：:]|语音识别|补上一条|补问题|往上读|自身身份|指定账号|登录账号|会话列表|→|←",text):
        return text.split("：")[0].split("→")[0].split("←")[0][:35]+" [私密内容已省略]"
    text=re.sub(r"wxid_[\w-]+|[\w.+-]+@[\w.-]+\.\w+|\d+@chatroom","[身份已脱敏]",text)
    return re.sub(r"Bearer\s+[^\s\"']+|sk-[\w-]{8,}","[凭据已省略]",text,flags=re.I)
def append(path,line,max_bytes=5*1024*1024):
    path=Path(path)
    with _LOCK:
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and path.stat().st_size>=max_bytes:
            for index in range(3,0,-1):
                source=path if index==1 else Path(str(path)+'.'+str(index-1))
                if source.exists():source.replace(Path(str(path)+'.'+str(index)))
        with path.open('a',encoding='utf-8') as stream:stream.write(line+'\n')
