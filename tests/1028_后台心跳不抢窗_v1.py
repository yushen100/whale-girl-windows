"""验证心跳和启动预热没有主动窗口操作。"""
import ast,unittest
from pathlib import Path
SRC=ast.parse((Path(__file__).resolve().parents[1]/'examples/wechatauto_bridge.py').read_text('utf-8'))
class Tests(unittest.TestCase):
 def check_passive(self,name):
  fn=next(n for n in ast.walk(SRC) if isinstance(n,ast.FunctionDef) and n.name==name)
  calls=[ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n,ast.Call)]
  for forbidden in ['_get_uia','recover_wechat_window','create_wechat_gui','force_foreground','ShowWindow','SetWindowPos']:
   self.assertFalse(any(forbidden in c for c in calls),(name,forbidden,calls))
 def test_heartbeat_passive(self):self.check_passive('bridge_heartbeat')
 def test_warmup_passive(self):self.check_passive('warmup')
if __name__=='__main__':unittest.main()
