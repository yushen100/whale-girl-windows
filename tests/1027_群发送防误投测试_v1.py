"""群发送误投回归测试：全部使用假 UI 和假数据库，不发送真实消息。"""
from pathlib import Path
import importlib.util
import sys
import unittest

spec=importlib.util.spec_from_file_location("exact_group",Path(__file__).resolve().parents[1]/"examples/exact_group_sender.py")
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)


class FakeUI:
    def __init__(self, current="107"):
        self.current=current
        self.pasted=[]
        self.keys=[]
        self.change_on_paste=False
        self.opened_name="107"
        self.landed=True
    def current_chat(self):return self.current
    def open_chat(self,name,**kwargs):
        self.current=self.opened_name
        return True
    def _chat_input(self):return self
    def _read_edit_value(self,entry):return self.pasted[-1] if self.pasted else ""
    def _paste_into(self,entry,text,clear):
        self.pasted.append(text)
        if self.change_on_paste:self.current="private-person"
    def _paste_landed(self,entry,text):return self.landed,text,1.0
    def SendKeys(self,key,**kwargs):self.keys.append(key)


class FakeDB:
    def __init__(self):
        self.contacts=[{"username":"room@chatroom","nick_name":"107","remark":""}]
        self.reads=0
        self.confirm=True
        self.receipt_sender="bot"
    def group_id_to_name(self,group):return "107"
    def search_contact(self,name):return self.contacts
    def get_messages(self,group,limit):
        assert group=="room@chatroom"
        self.reads+=1
        if self.reads>1 and self.confirm:
            return [{"sort_seq":2,"content":"test","sender_username":self.receipt_sender}]
        return [{"sort_seq":1,"content":"old","sender_username":"other"}]


class Tests(unittest.TestCase):
    def send(self,u=None,db=None,target="room@chatroom"):
        return module.send_exact_group(u or FakeUI(),db or FakeDB(),target,"test","bot",sleep=lambda _:None)
    def test_exact_group_confirmed_once(self):
        u=FakeUI();r=self.send(u)
        self.assertTrue(r.confirmed);self.assertEqual(u.keys,["{Enter}"])
    def test_private_target_refused_before_input(self):
        u=FakeUI();r=self.send(u,target="private-id")
        self.assertFalse(r.confirmed);self.assertEqual(u.pasted,[])
    def test_member_or_fuzzy_result_refused(self):
        for name in ["member","107-user","群107"]:
            u=FakeUI("private-person");u.opened_name=name
            self.assertFalse(self.send(u).attempted);self.assertEqual(u.pasted,[])
    def test_duplicate_group_private_name_refused(self):
        u=FakeUI();db=FakeDB();db.contacts.append({"username":"friend","nick_name":"107"})
        self.assertFalse(self.send(u,db).attempted);self.assertEqual(u.pasted,[])
    def test_chat_switch_during_paste_blocks_enter(self):
        u=FakeUI();u.change_on_paste=True
        self.assertFalse(self.send(u).attempted);self.assertEqual(u.keys,[])
    def test_no_receipt_never_resends(self):
        u=FakeUI();db=FakeDB();db.confirm=False
        r=self.send(u,db)
        self.assertTrue(r.attempted);self.assertFalse(r.confirmed);self.assertEqual(u.keys,["{Enter}"])
    def test_receipt_from_other_sender_is_not_success(self):
        u=FakeUI();db=FakeDB();db.receipt_sender="other"
        r=self.send(u,db)
        self.assertFalse(r.confirmed);self.assertEqual(u.keys,["{Enter}"])
    def test_paste_mismatch_never_submits(self):
        u=FakeUI();u.landed=False
        self.assertFalse(self.send(u).attempted);self.assertEqual(u.keys,[])


class BindingTests(unittest.TestCase):
    def test_search_static_method_keeps_original_arguments(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"examples"))
        import uia_compat
        class Driver:
            @staticmethod
            def search_button(window):return window
            def search_box(self,window):return window
        import inspect
        for name in ["search_button","search_box"]:
            descriptor=inspect.getattr_static(Driver,name)
            kind=staticmethod if isinstance(descriptor,staticmethod) else None
            original=descriptor.__func__ if kind else descriptor
            wrapped=uia_compat.wrap_step(original,name)
            setattr(Driver,name,kind(wrapped) if kind else wrapped)
        window=object()
        self.assertIs(Driver().search_button(window),window)
        self.assertIs(Driver().search_box(window),window)

if __name__=="__main__":unittest.main()
