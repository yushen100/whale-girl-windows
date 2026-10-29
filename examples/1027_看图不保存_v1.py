# -*- coding: utf-8 -*-
"""看图不要点「保存」——点开预览就够了，看完直接关窗。

现象（主人反馈）
    桥接看图时会点开图片气泡，这没问题；但接着它会去点预览窗的「保存」，
    弹出 Windows 通用另存为对话框后就卡在那儿不动了。之后更糟：
      · 看图窗关不掉（模态框把预览窗的消息循环堵住了）；
      · 微信主窗被模态框禁用，`IsWindowEnabled` 变假，
        于是「窗口是否属于机器人账号」的校验失败，连还原窗口都被跳过；
      · 发送通道直接瘫掉，日志里是「群消息未发送：微信主窗被弹窗禁用」。

根因
    `MediaDownloader.download_image_original()` 在等不到 `_h.dat` 落盘时，
    会退一步调 `_save_via_button()`：点预览窗的「保存」，把微信写出的明文
    收下来。这条兜底本来是给「原图档一直不落盘」的极端情况准备的，
    但它在真实环境里的代价是弹一个模态对话框——为了拿一张图，
    把整条发送通道堵死，完全不划算。

做法
    把 `_save_via_button` 变成空操作（返回 None）。
    上层 `download_image_original()` 拿不到就会返回 None，桥接照原来的逻辑
    退回「只读本机缓存」（`download_image` 的 full / best 档）。
    实测这条退路是好的：日志里 `图片档位：full → 45 KB` 之后识别照样成功。

    换句话说：**点开预览能拿到就拿到，拿不到就用本机那份，绝不去点保存。**

安全
    只改本进程内的行为，不碰微信目录、不写任何文件、不动第三方库源码。
"""

from __future__ import annotations


def install_no_save_patch(log=print):
    """给 MediaDownloader 关掉「点保存」这条兜底。返回打了几处补丁。"""
    try:
        from wechatauto.media import MediaDownloader
    except Exception as e:
        log("看图不保存补丁没装上（找不到 MediaDownloader）：%r" % (e,))
        return 0

    if getattr(MediaDownloader, "_whale_no_save", False):
        return 0

    def _save_via_button(self, win, save_dir, stem, deadline, t0,
                         ref_thumb=None, ref_mid=None, attempts=2):
        """空操作：不点「保存」，不弹另存为对话框。

        为什么连「取消」都不做：这个方法一被调用，说明上层已经决定要点保存了。
        在这里直接返回 None，上层会自己走「取不到 → 用本机缓存」的退路，
        对话框从头到尾不会出现，也就不存在「关了对话框还得再等它消失」的问题。
        """
        if callable(log):
            try:
                log("跳过「保存」兜底（不弹另存为对话框），改用本机缓存那一份")
            except Exception:
                pass
        return None

    MediaDownloader._save_via_button = _save_via_button
    MediaDownloader._whale_no_save = True
    return 1
