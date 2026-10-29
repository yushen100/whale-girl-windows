"""窗口收尾回归：模拟失败、超时和多账号，不向微信发送消息。"""
import ast,sys,unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"examples"))
from window_cleanup import cleanup_window
class Tests(unittest.TestCase):
 def run_case(self, **values):
  api=Mock();snap=dict(hwnd=10,visible=True,iconic=False,topmost=False,rect=(1,2,501,402));snap.update(values)
  self.assertTrue(cleanup_window(snap,lambda h:h==10,api=api));return api
 def test_failure_restores_minimized(self):self.run_case(iconic=True).ShowWindowAsync.assert_called_once_with(10,6)
 def test_failure_restores_hidden(self):self.run_case(visible=False).ShowWindowAsync.assert_called_once_with(10,0)
 def test_visible_window_kept(self):self.run_case().ShowWindowAsync.assert_not_called()
 def test_remove_temporary_topmost(self):self.assertEqual(self.run_case().SetWindowPos.call_args_list[0].args[1],-2)
 def test_user_topmost_preserved(self):self.assertEqual(self.run_case(topmost=True).SetWindowPos.call_count,1)
 def test_other_account_not_touched(self):
  api=Mock();self.assertFalse(cleanup_window({'hwnd':10},lambda h:False,api=api));self.assertEqual(api.mock_calls,[])
 def test_ownership_changed_after_demote(self):
  api=Mock();owns=Mock(side_effect=[True,False]);self.assertFalse(cleanup_window({'hwnd':10,'iconic':True},owns,api=api));api.ShowWindowAsync.assert_not_called()
 def test_success_minimizes(self):
  api=Mock();cleanup_window({'hwnd':10,'visible':True},lambda h:True,minimize=True,api=api);api.ShowWindowAsync.assert_called_once_with(10,6)
 def test_timeout_cleans_before_exit(self):
  src=ast.parse((Path(__file__).resolve().parents[1]/'examples/wechatauto_bridge.py').read_text('utf-8'))
  fn=next(n for n in ast.walk(src) if isinstance(n,ast.FunctionDef) and n.name=='send_watchdog')
  code=ast.unparse(fn);self.assertLess(code.index('restore_win_state('),code.index('os._exit('))
 def test_no_shared_input_queue(self):
  src=ast.parse((Path(__file__).resolve().parents[1]/'examples/wechatauto_bridge.py').read_text('utf-8'))
  fn=next(n for n in ast.walk(src) if isinstance(n,ast.FunctionDef) and n.name=='force_foreground')
  self.assertNotIn('AttachThreadInput',ast.unparse(fn))
if __name__=='__main__':unittest.main()
