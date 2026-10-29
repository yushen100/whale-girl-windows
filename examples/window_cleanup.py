"""微信发送收尾：异步窗口操作，不抢焦点、不关闭微信。"""
import ctypes
from ctypes import wintypes


def cleanup_window(snapshot, owns_window, minimize=False, api=None):
    if not snapshot or not snapshot.get("hwnd"):
        return False
    hwnd = snapshot["hwnd"]
    if api is None:
        api = ctypes.windll.user32
        api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        api.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    # 每次动作前重新核对，防止微信重启后句柄被复用到其他账号。
    if not owns_window(hwnd):
        return False
    if not snapshot.get("topmost", False):
        api.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x4000 | 0x0010 | 0x0001 | 0x0002)
    if not owns_window(hwnd):
        return False
    # 原来藏在托盘的恢复隐藏；原来最小化的恢复最小化。
    # 失败时原本就在前台的窗口保留，避免打断用户正在使用的微信。
    if snapshot.get("visible") is False:
        api.ShowWindowAsync(hwnd, 0)
    elif minimize or snapshot.get("iconic", False):
        api.ShowWindowAsync(hwnd, 6)
    elif snapshot.get("rect"):
        left, top, right, bottom = snapshot["rect"]
        api.SetWindowPos(hwnd, 0, left, top, right-left, bottom-top, 0x4000 | 0x0010 | 0x0004)
    return True
