"""及时回复等待测试：不访问网络、不发送微信消息。"""
import importlib.util
from pathlib import Path
import time
import unittest

spec=importlib.util.spec_from_file_location("progress",Path(__file__).resolve().parents[1]/"examples/progress_reply.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Tests(unittest.TestCase):
    def test_fast_reply_does_not_send_progress(self):
        calls=[]
        value=module.wait_for_response(lambda:"answer",lambda:calls.append(1),delay=.02,total=.1)
        self.assertEqual(value,"answer");self.assertEqual(calls,[])

    def test_slow_reply_sends_progress_once_then_returns_answer(self):
        calls=[]
        def request():
            time.sleep(.05);return "answer"
        value=module.wait_for_response(request,lambda:calls.append("progress"),delay=.01,total=.2)
        self.assertEqual(value,"answer");self.assertEqual(calls,["progress"])

    def test_timeout_does_not_wait_for_late_request_or_send_twice(self):
        calls=[]
        def request():
            time.sleep(.2);return "late"
        started=time.monotonic()
        with self.assertRaises(TimeoutError):
            module.wait_for_response(request,lambda:calls.append(1),delay=.01,total=.06)
        self.assertLess(time.monotonic()-started,.16)
        time.sleep(.21)
        self.assertEqual(calls,[1])

    def test_progress_failure_does_not_drop_final_answer(self):
        def request():
            time.sleep(.04);return "answer"
        def progress():
            raise RuntimeError("test")
        self.assertEqual(module.wait_for_response(request,progress,delay=.01,total=.2),"answer")

    def test_request_error_is_reported(self):
        def request():raise ValueError("test")
        with self.assertRaises(ValueError):
            module.wait_for_response(request,lambda:None,delay=.01,total=.1)


if __name__=="__main__":unittest.main()
