#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鲸鲸 · 微信 4.x 桥接（不注入、不修改微信）

原理：
  - 读消息：解密微信 4.x 本地 SQLite 数据库（只读，需要微信处于登录状态）
  - 发消息：驱动微信窗口（UI 自动化 + 剪贴板），相当于有人替你打字发送

前置：
  1. WeChat 4.x 已登录（建议用小号）
  2. 已装依赖：pip install wechatauto-replica requests
  3. 鲸鲸服务已启动（双击 启动鲸鱼娘.bat）
  4. config.json 里 gateway.wechat.enabled = true（脚本会读 token）

用法：
  python examples/wechatauto_bridge.py

注意：
  - 发送消息会抢微信窗口焦点，这是 4.x 自动化的正常表现。
  - 微信必须保持登录；退出登录后数据库密钥会失效。
  - 自动化不是微信官方接口，仍有账号风险，只用小号 + 测试群。
"""

import json
import random
import urllib.parse
import os
# UIA返回物理像素；在导入第三方界面库前统一每显示器DPI，防止控件点击偏移。
try:
    import ctypes
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except (AttributeError, OSError):
    pass
# 关掉 wechatauto 的「拟人节流」（默认 natural：120 秒内 6 次写就冷却 30~75 秒）
os.environ.setdefault("WECHATAUTO_RHYTHM", "off")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
LOG_FILE = os.path.join(ROOT, "data", "wechat-bridge-4x.log")

# 只回复这些会话（留空 = 全部会话都交给鲸鲸判断）
# 例如 ["文件传输助手", "深海鱼塘"]
ONLY_CHATS = []

# 距离启动多久之前的消息不处理（秒）
SKIP_OLDER_THAN = 10
DIGEST_MINUTES = 30
MASTER_WXID = ''
# 超过这么多秒的旧消息直接不回，避免“回上一条”
STALE_AFTER = float(os.environ.get("WHALE_STALE_AFTER", "4"))
_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
# 新版发行包不再自带 config.json（免得覆盖安装时把用户的主人名单/token 冲掉）。
# 如果用户先双击了桥接、服务还没起过，这里用 example 生成一份，避免「token 空 → 401 → 她不回话」。
if not os.path.exists(_cfg_path):
    _example = os.path.join(os.path.dirname(_cfg_path), "config.example.json")
    if os.path.exists(_example):
        try:
            import shutil
            shutil.copyfile(_example, _cfg_path)
            log_call = True
        except Exception:
            pass
try:
    with open(_cfg_path, "r", encoding="utf-8-sig") as _f:
        _cfg = json.load(_f)
    _stale = (_cfg.get("wechat") or {}).get("staleAfterSec")
    if isinstance(_stale, (int, float)) and _stale > 0:
        STALE_AFTER = float(_stale)
    _wechat = _cfg.get("wechat") or {}
    GAME_TOKENS = [str(x).lower() for x in (_wechat.get("gameWhitelist") or []) if str(x).strip()]
    AWAY_NOTICE = bool(_wechat.get("awayNotice", True))
    RETURN_NOTICE = bool(_wechat.get("returnNotice", True))
    QUEUED_MAX_AGE = float(_wechat.get("queuedMaxAgeSec", 300))
    QUEUED_MAX_CHATS = int(_wechat.get("queuedMaxChats", 2))
    QUOTE_REPLY = bool(_wechat.get("quoteReply", True))
    # UIA 彻底发不出去时，退到「Ctrl+F 搜索会话 → 粘贴 → 回车」的键盘通道（会抢前台）
    KEYBOARD_FALLBACK = bool(_wechat.get("keyboardFallback", True))
    # 后台发送：不抢前台直接发（实测最小化/被遮挡也能发）。false = 每次先把微信拉到前台
    BACKGROUND_SEND = bool(_wechat.get("backgroundSend", True))
    LOW_BATTERY_PCT = float(_wechat.get("lowBatteryPercent", 5))
    GOODNIGHT_TEXT = str(_wechat.get("goodnightText", "晚安，电量快没了，我先睡啦，明天见～"))
    _share = _cfg.get("share") or {}
    SHARE_ENABLED = bool(_share.get("enabled", True))
    SHARE_EVERY = float(_share.get("everyMinutes", 120))
    # 主动搭话（问「在干啥 / 累不累」）。时间窗和频率由服务端 /api/care/due 算，这里只留一个开关和静默期
    _care = _cfg.get("care") or {}
    CARE_ENABLED = bool(_care.get("enabled", True))
    CARE_QUIET_MIN = float(_care.get("quietAfterMinutes", 20) or 0)
    _digest = _wechat.get("digestMinutes", 30)
    DIGEST_MINUTES = float(_digest) if isinstance(_digest, (int, float)) else 30.0
    MASTER_WXID = (((_cfg.get("persona") or {}).get("master") or {}).get("wxids") or [''])[0]
except Exception:
    pass

START_TS = time.time()


def reload_config(path=None):
    """把 config.json 重新读一遍，覆盖上面这些「启动时定型」的开关。

    为什么需要：调试台（/debug）改的是 config.json，但桥接以前只在启动时读一次，
    用户在页面上关掉「主动分享」、改「忽略名单」、调「游戏白名单」……全都没反应，
    只能手动重启「启动微信桥接.bat」，表现出来就是「调了没用，还是默认」。
    现在 config.json 一变，5 秒内自动生效。

    返回是否真的重新加载了。
    """
    global GAME_TOKENS, AWAY_NOTICE, RETURN_NOTICE, QUEUED_MAX_AGE, QUEUED_MAX_CHATS
    global QUOTE_REPLY, KEYBOARD_FALLBACK, BACKGROUND_SEND, LOW_BATTERY_PCT, GOODNIGHT_TEXT
    global SHARE_ENABLED, SHARE_EVERY, CARE_ENABLED, CARE_QUIET_MIN, DIGEST_MINUTES
    global STALE_AFTER, SYSTEM_SENDER_NAMES, BASE_SYSTEM_SENDERS, MASTER_WXID
    global VOICE_ENABLED, VOICE_BASE_URL, VOICE_MODEL, VOICE_API_KEY, VOICE_TIMEOUT, VOICE_MAX_SECONDS
    target = path or CONFIG_PATH
    try:
        with open(target, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception as e:                      # noqa: BLE001
        log("重读配置失败（继续用旧值）：", repr(e))
        return False

    w = cfg.get("wechat") or {}
    try:
        STALE_AFTER = float(w.get("staleAfterSec") or STALE_AFTER or 4)
    except Exception:
        pass
    GAME_TOKENS = [str(x).lower() for x in (w.get("gameWhitelist") or []) if str(x).strip()]
    AWAY_NOTICE = bool(w.get("awayNotice", True))
    RETURN_NOTICE = bool(w.get("returnNotice", True))
    try:
        QUEUED_MAX_AGE = float(w.get("queuedMaxAgeSec", 300))
        QUEUED_MAX_CHATS = int(w.get("queuedMaxChats", 2))
        LOW_BATTERY_PCT = float(w.get("lowBatteryPercent", 5))
    except Exception:
        pass
    QUOTE_REPLY = bool(w.get("quoteReply", True))
    KEYBOARD_FALLBACK = bool(w.get("keyboardFallback", True))
    BACKGROUND_SEND = bool(w.get("backgroundSend", True))
    GOODNIGHT_TEXT = str(w.get("goodnightText") or GOODNIGHT_TEXT)
    try:
        DIGEST_MINUTES = float(w.get("digestMinutes", 30))
    except Exception:
        DIGEST_MINUTES = 30.0
    # 忽略名单：系统会话是固定底表，配置里的只追加
    SYSTEM_SENDER_NAMES = set(BASE_SYSTEM_SENDERS)
    SYSTEM_SENDER_NAMES |= {str(x).strip() for x in (w.get("ignoreSenders") or []) if str(x).strip()}

    sh = cfg.get("share") or {}
    SHARE_ENABLED = bool(sh.get("enabled", True))
    try:
        SHARE_EVERY = float(sh.get("everyMinutes", 120))
    except Exception:
        SHARE_EVERY = 120.0

    ca = cfg.get("care") or {}
    CARE_ENABLED = bool(ca.get("enabled", True))
    try:
        CARE_QUIET_MIN = float(ca.get("quietAfterMinutes", 20) or 0)
    except Exception:
        CARE_QUIET_MIN = 20.0

    vo = cfg.get("voice") or {}
    if "enabled" in vo:
        VOICE_ENABLED = bool(vo.get("enabled"))
    if vo.get("baseUrl"):
        VOICE_BASE_URL = str(vo.get("baseUrl"))
    if vo.get("model"):
        VOICE_MODEL = str(vo.get("model"))
    # 语音这几项以前只在启动时读一次：网页里换了 Key / 调了超时，必须重启才生效
    try:
        if vo.get("timeoutMs") is not None:
            VOICE_TIMEOUT = float(vo.get("timeoutMs")) / 1000.0
        if vo.get("maxSeconds") is not None:
            VOICE_MAX_SECONDS = float(vo.get("maxSeconds"))
    except Exception:
        pass
    VOICE_API_KEY = _resolve_voice_key(cfg)

    wxids = (((cfg.get("persona") or {}).get("master") or {}).get("wxids") or [])
    if wxids:
        MASTER_WXID = wxids[0]
    return True


# 系统 / 公众号通知：只用来记录，不接话（否则会跟「微信团队」吵起来）
SYSTEM_SENDER_NAMES = {
    "微信团队", "微信支付", "服务通知", "订阅号消息", "文件传输助手",
    "微信运动", "微信公众平台", "腾讯客服", "微信安全中心", "微信广告",
}
try:
    SYSTEM_SENDER_NAMES |= {str(x).strip() for x in (_wechat.get("ignoreSenders") or []) if str(x).strip()}
except Exception:
    pass
# 系统会话名单的原始底表（热更新时以它为基准重新算，避免配置里的旧值越滚越多）
BASE_SYSTEM_SENDERS = set(SYSTEM_SENDER_NAMES)

# 微信 4.x 数据目录的常见名字（不同版本 / 不同安装方式会有差异）
DB_ROOT_NAMES = ("xwechat_files", "WeChat Files", "xwechat_files_data")


def looks_like_db_root(p):
    """这个目录像不像微信 4.x 的数据根（下面有 <账号>/db_storage）。

    兼容四种布局（以前只认第一种，文档写了后面几种却没实现）：
      E:\\xwechat_files\\<账号>\\db_storage
      E:\\xwechat_files\\db_storage            （账号目录层级被拍平）
      直接把 <账号> 目录填进来（p\\db_storage）
      直接把 db_storage 目录填进来
    """
    if not p or not os.path.isdir(p):
        return False
    # 直填账号目录 / 直填 db_storage
    if os.path.isdir(os.path.join(p, "db_storage")):
        return True
    if os.path.basename(os.path.normpath(p)).lower() == "db_storage":
        return True
    bases = [p]
    for name in DB_ROOT_NAMES:
        bases.append(os.path.join(p, name))
    for base in bases:
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for name in entries:
            if os.path.isdir(os.path.join(base, name, "db_storage")):
                return True
    return False


def iter_drive_roots():
    """列出真实的磁盘根（含 L:、网络映射盘、UNC），不再写死 C..K。"""
    roots = []
    try:
        import string
        for letter in string.ascii_uppercase:
            root = letter + ":\\"
            if os.path.isdir(root):
                roots.append(root)
    except Exception:
        pass
    try:
        import psutil
        for part in psutil.disk_partitions(all=False):
            mp = str(part.mountpoint or "")
            if mp and mp not in roots and os.path.isdir(mp):
                roots.append(mp)
    except Exception:
        pass
    return roots


def resolve_db_dir(config_path=None, drives="CDEFGHIJK"):
    """找微信 4.x 的数据库目录，比 wechatauto 自带的 auto_detect 更狠。

    库自带的只查「文档 / 用户目录 / 注册表」，用户把微信文件存到别的盘
    （很常见：E:\\xwechat_files、D:\\xwechat_files）就永远找不到，启动直接
    RuntimeError 崩掉 —— 也就是用户截图里那一整段 Traceback。

    顺序：config.json 的 wechat.dbDir → 正在运行的微信进程打开的文件 → 各盘根目录。
    """
    # 1) 配置里手填的
    manual = ""
    try:
        with open(config_path or CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            manual = str(((json.load(f).get("wechat") or {}).get("dbDir") or "")).strip()
    except Exception:
        manual = ""
    if manual and looks_like_db_root(manual):
        return manual

    # 2) 正在运行的微信进程打开着数据库文件 —— 直接反射出来，最准
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            name = str(proc.info.get("name") or "").lower()
            if not any(k in name for k in ("weixin", "wechat")):
                continue
            try:
                open_files = proc.open_files()
            except Exception:
                continue
            for item in open_files:
                m = re.search(r"([\\/])(" + "|".join(DB_ROOT_NAMES) + r")([\\/]|$)",
                              item.path or "", re.I)
                if not m:
                    continue
                # 取「名字之前的那一段」作为盘符/目录，再拼回 xwechat_files
                root = (item.path or "")[: m.start() + 1] + m.group(2)
                if looks_like_db_root(root):
                    return root
    except Exception:
        pass

    # 3) 所有真实磁盘根（以前写死 C..K，L: 盘和网络盘永远找不到）
    for root in iter_drive_roots():
        for name in DB_ROOT_NAMES:
            cand = os.path.join(root, name)
            if looks_like_db_root(cand):
                return cand
    return ""

# 系统 / 公众号通知：只用来记录，不接话（否则会跟「微信团队」吵起来）
SYSTEM_SENDER_NAMES = {
    "微信团队", "微信支付", "服务通知", "订阅号消息", "文件传输助手",
    "微信运动", "微信公众平台", "腾讯客服", "微信安全中心", "微信广告",
}
try:
    SYSTEM_SENDER_NAMES |= {str(x).strip() for x in (_wechat.get("ignoreSenders") or []) if str(x).strip()}
except Exception:
    pass
_SYSTEM_TEXT_RE = __import__("re").compile(r"<a\s+href=|weixin://|&lt;a\s+href=", __import__("re").I)
# 同一会话同一句话的重复回复保护（秒）与每分钟回复上限
DUP_REPLY_WINDOW = 45
MAX_REPLIES_PER_MIN = 8
# 单实例锁端口：被占用就说明已经有桥接在跑，直接退出
BRIDGE_LOCK_PORT = 47821

# 2026-10-27 加：她自己是谁。由 main() 里读 db 后填。
# 发给服务端，让模型知道群里那个「琦」就是自己，不是旁人。
SELF_WXID = ""
SELF_NICK = ""

# —— 语音识别（语音消息 → 文字）——
# 默认用阿里云百炼的 qwen3-asr-flash；Key 依次取 voice.apiKey → data/settings.json → deepseek.apiKey
VOICE_ENABLED = True
VOICE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
VOICE_MODEL = "qwen3-asr-flash"
VOICE_API_KEY = ""
VOICE_TIMEOUT = 90
VOICE_MAX_SECONDS = 120
VOICE_SAMPLE_RATE = 16000


def _resolve_voice_key(cfg):
    """语音 Key 的查找链：voice.apiKey → settings.voiceApiKey → settings.apiKey → config.deepseek.apiKey。

    以前只回退到 settings.apiKey，配置文件里写的 deepseek.apiKey 永远用不上（注释承诺了却没实现）；
    而且 reload_config 不刷新语音 Key，网页里换 Key 必须重启桥接才生效。
    """
    vo = (cfg.get("voice") or {}) if isinstance(cfg, dict) else {}
    key = str(vo.get("apiKey") or "").strip()
    if key:
        return key
    try:
        with open(os.path.join(ROOT, "data", "settings.json"), "r", encoding="utf-8-sig") as sf:
            st = json.load(sf)
        for name in ("voiceApiKey", "apiKey"):
            v = str(st.get(name) or "").strip()
            if v:
                return v
        keys = st.get("keys") or {}
        for name in ("vision", "qwen", "zhipu", "deepseek"):
            v = str(keys.get(name) or "").strip()
            if v:
                return v
    except Exception:
        pass
    try:
        v = str(((cfg.get("deepseek") or {}).get("apiKey") or "")).strip()
        if v:
            return v
    except Exception:
        pass
    return ""


try:
    _voice_cfg = (_cfg.get("voice") or {})
    VOICE_ENABLED = bool(_voice_cfg.get("enabled", True))
    VOICE_BASE_URL = str(_voice_cfg.get("baseUrl") or VOICE_BASE_URL)
    VOICE_MODEL = str(_voice_cfg.get("model") or VOICE_MODEL)
    VOICE_TIMEOUT = float(_voice_cfg.get("timeoutMs", 90000)) / 1000.0
    VOICE_MAX_SECONDS = float(_voice_cfg.get("maxSeconds", 120))
    VOICE_API_KEY = _resolve_voice_key(_cfg)
except Exception:
    pass


def asr_transcribe(wav_bytes, api_key, model, base_url, timeout=90):
    """把 wav 字节交给语音识别接口，返回文字；失败抛异常或返回空串。"""
    import base64
    import urllib.request

    if not api_key:
        raise RuntimeError("没有可用的语音识别 Key")
    payload = {
        "model": model,
        "input": {
            "messages": [{
                "role": "user",
                "content": [{"audio": "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode()}],
            }]
        },
    }
    req = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    try:
        text = data["output"]["choices"][0]["message"]["content"][0]["text"]
    except Exception:
        text = ""
    return str(text or "").strip()


def clean_transcript(text):
    """去掉只剩标点的转写结果。"""
    t = str(text or "").strip()
    t = t.strip("，,。.！!？?…~～、 ")
    return t if len(t) >= 1 else ""

_recent_dup = {}
_reply_stamps = {}
_guard_lock = threading.Lock()
# 「演示模式」提示只喊一次，别每条都刷
MOCK_NOTICED = {"done": False}


def is_system_notice(username, nickname, content):
    """系统号 / 通知类消息：带 weixin:// 链接、HTML 链接、或已知系统号名字。"""
    if _SYSTEM_TEXT_RE.search(str(content or "")):
        return True
    name = str(nickname or "").strip()
    if name in SYSTEM_SENDER_NAMES:
        return True
    uid = str(username or "")
    if uid.startswith("gh_"):
        return True
    return False


def duplicate_guard(chat_username, sender, content):
    """同一人 45 秒内重复同一句 → 不再回（微信重投/通知刷屏）。"""
    now = time.time()
    key = (str(chat_username or ""), str(sender or ""), str(content or "").strip())
    with _guard_lock:
        last = _recent_dup.get(key, 0)
        _recent_dup[key] = now
        if len(_recent_dup) > 500:
            for k, v in list(_recent_dup.items()):
                if now - v > 600:
                    _recent_dup.pop(k, None)
        return now - last < DUP_REPLY_WINDOW


def rate_guard(chat_username):
    """同一会话一分钟内已经回了 MAX_REPLIES_PER_MIN 条 → 先停下，避免越聊越乱。"""
    now = time.time()
    key = str(chat_username or "")
    with _guard_lock:
        stamps = [t for t in _reply_stamps.get(key, []) if now - t < 60]
        if len(stamps) >= MAX_REPLIES_PER_MIN:
            _reply_stamps[key] = stamps
            return False
        stamps.append(now)
        _reply_stamps[key] = stamps
    return True


BOOT_LOG = os.path.join(ROOT, "data", "bridge-boot.log")


# ---------------------------------------------------------------------------
# 故障告警：出事就在群里 @ 主人 3 次，然后宣布静默，等主人回话
#
# 主人 2026-10-27 的原话：
#   「遇到重要问题或者bug在群里@我,连续三次然后宣布自己静默直到我回复」
#
# 要点（都是主人踩过的坑，别改）：
#   · 只有「重要」故障才吵 —— 图片没加载、语音没听清这种属于日常噪音，
#     她自己扛过去就行，@ 主人三次会把人烦死。所以分 IMPORTANT / MINOR 两级。
#   · 同一种故障只吵一轮：3 次 @ 发完就进静默，直到主人在群里说话才解除，
#     绝不定时重播。
#   · 静默期间她**照常回复群友**（只是不再报警），不是整个人装死 ——
#     主人要的是「别刷屏」，不是「别上班」。
#   · 只在白名单群里 @，绝不去别的群喊人。
# ---------------------------------------------------------------------------
FAULT_LOCK = threading.Lock()
FAULT_STATE = {
    # 每种故障的现场：key -> {count, first_ts, last_ts, silenced, silenced_at, detail}
    "items": {},
    # 全局静默：报警期间为 True，主人回话后清除
    "silenced": False,
    "silenced_since": 0,
    "silence_chat": "",      # 在哪个群里宣布的静默（主人只在这个群说话才算「回复」）
    "silence_name": "",
    "last_notify": 0,        # 上次真发出 @ 的时间（防抖，两条故障别同时炸）
}
FAULT_NOTIFY_GAP = 25          # 两次 @ 之间至少隔这么久，别连着刷
FAULT_ECHO_WINDOW = 30 * 60    # 同一种故障 30 分钟内复发算「同一起」，不重新计数

# 重要故障：值得把主人从睡梦里叫起来的
FAULT_IMPORTANT = {
    "llm_down": "模型调用失败（连续多次，她真的接不上话了）",
    "send_fail": "消息发不出去（微信窗口/UIA 出问题）",
    "bridge_crash": "桥接异常重启",
    "db_error": "微信数据库读不了",
    "whitelist_error": "白名单判定出错（有泄漏风险）",
}


def fault_important(kind):
    """这个故障值不值得吵主人。"""
    return str(kind or "") in FAULT_IMPORTANT


def _fault_item(kind):
    now = time.time()
    with FAULT_LOCK:
        it = FAULT_STATE["items"].get(kind)
        if it is None or (now - it["last_ts"]) > FAULT_ECHO_WINDOW:
            it = {"count": 0, "first_ts": now, "last_ts": now,
                  "silenced": False, "silenced_at": 0, "detail": ""}
            FAULT_STATE["items"][kind] = it
        else:
            it["last_ts"] = now
        return it


def fault_note(kind, detail=""):
    """记一笔故障，返回「这次要不要 @ 主人」。

    只有同时满足这几条才报警：
      1. 属于重要故障（日常噪音直接忽略，只写日志）
      2. 现在没有处于静默期
      3. 这一轮还没吵过（一轮最多 3 次 @，然后进静默）
    """
    if not fault_important(kind):
        log("[小故障·不吵主人] %s：%s" % (kind, str(detail)[:80]))
        return False
    it = _fault_item(kind)
    if detail:
        it["detail"] = str(detail)[:200]
    with FAULT_LOCK:
        if FAULT_STATE["silenced"]:
            return False
        if it["silenced"]:
            return False
        it["count"] += 1
        return True


def fault_silenced():
    """现在是不是报警后的静默期（静默期间照常回复，只是不再报警）。"""
    with FAULT_LOCK:
        return bool(FAULT_STATE["silenced"])


def fault_master_replied(chat_username):
    """主人在群里说话了 → 解除静默，允许下一轮报警。

    只在「宣布静默的那个群」里说话才算，别的地方说话不作数 ——
    否则主人在另一个群随口一句，静默就被悄悄解除了。
    """
    with FAULT_LOCK:
        if not FAULT_STATE["silenced"]:
            return False
        want = FAULT_STATE.get("silence_chat") or ""
        if want and str(chat_username or "") != want:
            return False
        FAULT_STATE["silenced"] = False
        FAULT_STATE["silenced_since"] = 0
        for it in FAULT_STATE["items"].values():
            it["silenced"] = False
            it["count"] = 0
        return True


def boot(msg):
    """启动诊断：只写文件，不依赖控制台（控制台被选中时 print 会卡住）"""
    try:
        os.makedirs(os.path.dirname(BOOT_LOG), exist_ok=True)
        with open(BOOT_LOG, "a", encoding="utf-8") as f:
            f.write("%s [pid %s] %s\n" % (time.strftime("%H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass


boot("脚本开始执行")


def log(*args):
    from private_log import redact, append
    line = time.strftime("[%H:%M:%S] ") + redact(" ".join(str(a) for a in args))
    # 先写文件，再打印：这样即使控制台卡住，日志也不会丢
    try:
        append(LOG_FILE, line)
    except Exception:
        pass
    try:
        if sys.stdout.isatty() or os.environ.get("WHALE_CONSOLE_LOG")=="1":
            print(line, flush=True)
    except Exception:
        pass


_win_problem_ts = {}


def log_window_problem(where, err):
    """窗口操作失败两分钟内只报一次。

    挪不动/置顶不了窗口不该让整条发送失败（UIA 还能把字打进去），
    更不该让看门狗以为“发送卡住”把桥接反复重启——那会一直弹「拒绝访问」。
    """
    now = time.time()
    if now - _win_problem_ts.get(where, 0) < 120:
        return
    _win_problem_ts[where] = now
    log(where + "：", repr(err))


def read_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception as e:
            log("读取 config.json 失败：", e)
    return cfg


def build_bot_url(cfg):
    port = (cfg.get("server") or {}).get("port", 8787)
    return os.environ.get("WHALE_BOT_URL", "http://127.0.0.1:%s/webhook/wechat" % port)


def ensure_ffmpeg_env():
    """微信 4.x 的普通图片是 HEVC(wxgf) 容器，要 ffmpeg 才能转成 jpg。

    media 库用 shutil.which("ffmpeg") 找，很多机器装了 ffmpeg 但没加进 PATH，
    结果图片一直转不出来、看图全是「图片输入格式/解析错误」。
    这里先自己找一遍常见位置，找到就塞进 PATH。
    """
    import shutil
    if shutil.which("ffmpeg"):
        return True
    cands = [
        os.path.join(ROOT, "runtime", "ffmpeg", "bin"),
        os.path.join(ROOT, "tools", "ffmpeg", "bin"),
        r"C:\ffmpeg\bin", r"D:\ffmpeg\bin", r"E:\ffmpeg\bin", r"F:\ffmpeg\bin",
        os.path.expanduser(r"~\ffmpeg\bin"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\ffmpeg\bin"),
        r"C:\Program Files\ffmpeg\bin", r"D:\Program Files\ffmpeg\bin",
    ]
    for d in cands:
        exe = os.path.join(d, "ffmpeg.exe")
        if os.path.isfile(exe):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            log("找到 ffmpeg：", exe, "（微信图片是 wxgf/HEVC，靠它转 jpg）")
            return True
    log("[!] 没找到 ffmpeg：微信 4.x 的图片多是 wxgf/HEVC，没有它就看不了图。")
    log("    装一个（解压到 C:\\ffmpeg 即可）或把 ffmpeg.exe 放进 runtime\\ffmpeg\\bin")
    return False


def main():
    boot("进入 main()")
    ensure_ffmpeg_env()
    # 微信的 Qt accessibility 只在「纯英文路径」下能热激活成功；路径里带中文时，
    # UIA 认不出主窗（ensure_window 超时 37s），之后每次发送都退到 OCR 并报
    # error(5, 'SetWindowPos', '拒绝访问')，日志刷屏、消息发不出去。
    if any(ord(ch) > 127 for ch in ROOT):
        log("[!] 路径里有中文/非英文字符：", ROOT)
        log("    微信 UI 自动化在这种路径下会失败（发送一直报「拒绝访问」）。")
        log("    请把整个文件夹移到纯英文路径再启动，例如 D:\\whale-girl-3.0")
    # 单实例锁：多个桥接连同一份微信数据库会导致同一条消息被回好几遍
    _lock_sock = None
    try:
        import socket as _socket
        _lock_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _lock_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 0)
        _lock_sock.bind(("127.0.0.1", BRIDGE_LOCK_PORT))
        _lock_sock.listen(1)
        boot("单实例锁已获取（端口 %d）" % BRIDGE_LOCK_PORT)
    except OSError as e:
        log("已经有一个桥接在跑了（端口 %d 被占用），本进程退出：%r" % (BRIDGE_LOCK_PORT, e))
        try:
            if _lock_sock:
                _lock_sock.close()
        except Exception:
            pass
        return 0
    except Exception:
        _lock_sock = None
    try:
        import faulthandler

        try:
            _hang_file = open(os.path.join(ROOT, "data", "bridge-hang.log"), "a", encoding="utf-8")
            # 仅在实际界面任务开始后启用一次超时堆栈，空闲不刷日志。
            faulthandler.cancel_dump_traceback_later()
            boot("已开启卡死堆栈转储（40s）")
        except Exception:
            pass

        # 原生崩溃（0xC0000005）发生时，让 faulthandler 尽量在死前吐出当时正在跑哪句，
        # 写进 data/bridge-crash.log。能吐出来就省得猜是 UIA 还是别的什么崩的。
        try:
            _crash_file = open(os.path.join(ROOT, "data", "bridge-crash.log"), "a", encoding="utf-8")
            _crash_file.write("\n==== %s 进程启动（pid=%d）====\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
            _crash_file.flush()
            faulthandler.enable(file=_crash_file, all_threads=True)
            boot("已开启崩溃堆栈记录（data/bridge-crash.log）")
        except Exception:
            pass

        boot("开始 import requests")
        import requests

        boot("开始 import wechatauto（可能较慢，10~20 秒）")
        from wechatauto import MediaDownloader, WeChatDB, WeChatGUI
        from wechatauto.db import Listener

        # 小版本兼容补丁：本机微信上 UIA 控件树其实已经物化，但顶层窗口类名不是
        # mmui::MainWindow，wechatauto 自带的判断会把它当成「没物化」，于是每次
        # 发送都跳过 UIA、掉进会卡死在截屏里的 OCR 兜底。补丁见 uia_compat.py。
        # 包里自带的 Python 开了 safe_path（sys.flags.safe_path=True，sys.path[0]
        # 是 python312.zip 而不是脚本目录），所以同目录的 uia_compat 直接 import 会
        # ModuleNotFoundError。这里显式把 examples 目录塞进 sys.path；再不行就按
        # 文件路径加载（双保险）。
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        # 打开每一步的耗时日志（uia step xxx -> X.XXs），下次卡住能直接看出停在哪步
        os.environ.setdefault("WHALE_STEPLOG", "0")
        import logging
        logging.getLogger("wechatauto").setLevel(logging.WARNING)
        logging.getLogger("wechatauto.uia_compat").setLevel(logging.WARNING)
        from private_log import redact as redact_diagnostic
        class PrivateDiagnostic(logging.Filter):
            def filter(self,record):
                record.msg=redact_diagnostic(record.getMessage())
                record.args=()
                return True
        from logging.handlers import RotatingFileHandler
        library_logger=logging.getLogger("wechatauto")
        for handler in list(library_logger.handlers):
            library_logger.removeHandler(handler)
            handler.close()
        library_handler=RotatingFileHandler(os.path.join(ROOT,"data","wechat-library.log"),maxBytes=5*1024*1024,backupCount=3,encoding="utf-8")
        library_handler.addFilter(PrivateDiagnostic())
        library_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        library_logger.addHandler(library_handler)
        library_logger.propagate=False

        boot("应用 uia_compat 补丁")
        try:
            import uia_compat
            boot("uia_compat.apply() = %r" % (uia_compat.apply(),))
        except Exception as _compat_err:
            try:
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location(
                    "uia_compat", os.path.join(_here, "uia_compat.py"))
                _mod = _ilu.module_from_spec(_spec)
                sys.modules["uia_compat"] = _mod
                _spec.loader.exec_module(_mod)
                _mod.apply()
                boot("uia_compat 已按文件路径加载并生效")
            except Exception:                # noqa: BLE001
                boot("uia_compat 未生效: %r" % (_compat_err,))

        boot("import 完成")
    except Exception as e:
        boot("导入失败: %r" % (e,))
        log("缺少依赖或导入失败：", e)
        log("请先运行：\"%LOCALAPPDATA%\\Programs\\Python\\Python312\\python.exe\" -m pip install wechatauto-replica requests")
        return 1

    cfg = read_config()
    gw = (cfg.get("gateway") or {}).get("wechat") or {}
    token = gw.get("inboundToken", "")
    bot_url = build_bot_url(cfg)
    aliases = ((cfg.get("persona") or {}).get("aliases") or []) + [
        ((cfg.get("persona") or {}).get("name") or "鲸鲸")
    ]

    log("连接微信 4.x 数据库（只读）…")
    # token 一定要读到：读不到就是「她不理人」最隐蔽的原因（服务端会直接 401）
    if token:
        log("webhook token：已读取（%s…）" % token[:6])
    else:
        log("⚠️ webhook token 是空的！检查 config.json 的 gateway.wechat.inboundToken（文件要能正常解析）")
    db_dir = resolve_db_dir()
    if db_dir:
        log("数据库目录：", db_dir)
    try:
        # 2026-10-27 改（主人同时登着主号和小号）：
        #   WeChatDB 默认按「数据库最后修改时间」挑账号，两个号同时在跑时会挑错，
        #   很可能挑到主号示例身份15。必须用 wechat.account 显式指定小号。
        _account = str((cfg.get("wechat") or {}).get("account") or "").strip()
        if db_dir and _account:
            db = WeChatDB(db_dir=db_dir, account=_account)
            log("已指定账号：%s" % _account)
        elif db_dir:
            db = WeChatDB(db_dir=db_dir)
        else:
            db = WeChatDB()
    except RuntimeError as e:
        # 不甩 traceback：告诉用户接下来该点哪里
        log("")
        log("✗ 找不到微信的数据库目录，读不到消息。按顺序检查：")
        log("  1) 微信 4.x 是不是已经登录了？（停在扫码/登录界面时还没有数据库）")
        log("  2) 微信 → 左下角三条杠 → 设置 → 文件管理 → 「微信文件默认保存位置」")
        log("     把那个路径填进 config.json：\"wechat\": { \"dbDir\": \"E:\\\\xwechat_files\" }")
        log("     （填到 xwechat_files 这一层，不是里面的 wxid_demo52b40cb2 目录）")
        log("  3) 保存后重新运行「启动微信桥接.bat」")
        log("  原始错误：%s" % e)
        return 2

    # 2026-10-27 加（主人有两个号，必须别连错）：
    #   只允许白名单里的 wxid 连接。主号 BEIGUANG示例身份15 永远不能连 ——
    #   连上去就等于用主号发言，这是明确禁止的。
    #   白名单在 config.json 的 wechat.allowWxids；留空 = 不限制（但会警告）。
    allow = [str(x).strip() for x in (cfg.get("wechat") or {}).get("allowWxids") or [] if str(x).strip()]
    deny = [str(x).strip() for x in (cfg.get("wechat") or {}).get("denyWxids") or [] if str(x).strip()]
    self_wxid = ""
    try:
        self_wxid = str(getattr(db, "wxid", "") or getattr(db, "account", "") or "")
        if not self_wxid.startswith("wxid_"):
            self_wxid = str(getattr(db, "account", "") or "")
    except Exception:
        pass
    log("当前登录账号：%s" % (self_wxid or "（读不到）"))
    if self_wxid and deny and any(self_wxid == d for d in deny):
        log("")
        log("✗ 当前登录的是【禁用账号】%s，桥接拒绝启动。" % self_wxid)
        log("  这是主号，不能用来发言。请换小号登录微信后重试。")
        return 3
    if allow and not any(self_wxid == a for a in allow):
        log("")
        log("✗ 当前登录账号 %s 不在白名单里，桥接拒绝启动。" % (self_wxid or "（未知）"))
        log("  白名单：%s" % "、".join(allow))
        log("  如果你确认要用这个号，把它的 wxid 加进 config.json 的 wechat.allowWxids。")
        return 3
    if not allow:
        log("⚠️ 没配置 wechat.allowWxids 白名单，任何账号都会连上。")
        log("   建议把要用的那个号的 wxid 填进去，防止连错号。")

    _media_box = {"downloader": None}

    def _get_media():
        """按需创建媒体下载器；图片和表情识别共用一个实例。"""
        if _media_box["downloader"] is None:
            save_dir = os.path.join(ROOT, "data", "media-cache")
            os.makedirs(save_dir, exist_ok=True)
            dl = MediaDownloader(db, save_dir=save_dir)
            # 微信改过「文件保存位置」后，图片缓存可能留在旧目录（msg/attach），而
            # 数据库已经搬到了新位置。这时只看 db.account_dir 图片就永远找不到：
            # 「图片识别只认默认微信文件地址，改了就不行」就是这个原因。
            # 加一层只读兜底：当前目录找不到，就按会话 md5 去其它账号目录/盘里找。
            try:
                from media_fallback import install_media_fallback
                installed = install_media_fallback(
                    dl, db, log=log,
                    root_names=DB_ROOT_NAMES,
                )
                if installed:
                    log("图片目录兜底已就位（%d 处补丁）" % installed)
            except Exception as _e:
                log("图片目录兜底没装上（默认路径不受影响）：", repr(_e))
            # 2026-10-27 加（主人：「点开图片后它会去点保存，弹出另存为就卡住不动了」）：
            #   拿原图时如果 _h.dat 没落盘，wechatauto 会退一步点预览窗的「保存」。
            #   那一下会弹 Windows 通用另存为对话框，而且是**模态**的：
            #     · 预览窗被堵住，关不掉；
            #     · 微信主窗 IsWindowEnabled 变假 → 账号归属校验失败 → 连还原都跳过；
            #     · 发送通道瘫掉，日志里是「群消息未发送：微信主窗被弹窗禁用」。
            #   为了一张图堵死整条发送链路，完全不划算。直接关掉这条兜底：
            #   点开预览能拿到就拿，拿不到就用本机缓存那份（实测够用）。
            try:
                # 按文件路径加载，不靠模块名 —— 文件名以数字开头，
                # import_module 那种按名字找的方式不稳妥。
                import importlib.util as _ilu
                _p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "1027_看图不保存_v1.py")
                _spec = _ilu.spec_from_file_location("whale_no_save", _p)
                _nosave = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_nosave)
                _n = _nosave.install_no_save_patch(log=log)
                if _n:
                    log("看图不再点「保存」：已关掉另存为对话框那条兜底")
            except Exception as _e:
                log("看图不保存补丁没装上（默认行为不受影响）：", repr(_e))
            _media_box["downloader"] = dl
        return _media_box["downloader"]

    me = db.wxid
    try:
        info = db.get_self_info() or {}
        log("当前登录账号：", info.get("nick_name") or info.get("remark") or me)
    except Exception:
        log("当前登录账号 wxid：", me)
    log("数据库目录：", db.db_dir)

    sessions = {}
    try:
        for s in db.get_sessions(limit=800):
            sessions[s["username"]] = s
    except Exception as e:
        log("读取会话列表失败：", e)

    name_cache = {}

    def display_name(username):
        if username in name_cache:
            return name_cache[username]
        name = ""
        try:
            if username.endswith("@chatroom"):
                name = db.group_id_to_name(username) or ""
                if not name:
                    s = sessions.get(username) or {}
                    name = s.get("last_sender") or ""
            else:
                name = db.get_nickname(username) or ""
        except Exception:
            name = ""
        name = name or username
        name_cache[username] = name
        return name

    alias_cache = {}
    # 上次把这个会话切到哪一行名字（微信里没命名的群，行名是成员名）
    open_cache = {}

    def aliases_for(username, shown=""):
        """会话列表里那一行可能叫什么（微信里没命名的群，显示的是成员名）。

        实测：群 9000000003@chatroom 在数据库里 nick_name 是空的，会话列表里显示
        「宇火之于、云白『世一区』」—— 按群名/open_chat 都匹配不上，所以这里把
        「成员名用、连起来」也当候选名。
        """
        key = (username or "", shown or "")
        if key in alias_cache:
            return alias_cache[key]
        out = []
        if shown and shown != username and not str(shown).endswith("@chatroom"):
            out.append(shown)
        if username and str(username).endswith("@chatroom"):
            try:
                nm = db.group_id_to_name(username) or ""
            except Exception:
                nm = ""
            if nm and nm != username:
                out.append(nm)
            try:
                nicks = []
                for m in (db.get_group_members(username) or []):
                    if m.get("username") == me:
                        continue
                    n = (m.get("remark") or "").strip() or (m.get("nick_name") or "").strip()
                    if n:
                        nicks.append(n)
                if nicks:
                    out.append("、".join(nicks))
            except Exception:
                pass
        if not out:
            out.append(shown or username)
        out = [x for x in dict.fromkeys(out) if x]
        alias_cache[key] = out
        return out

    def chat_latest_seq(username):
        try:
            rows = db.get_messages(username, limit=1)
            return rows[0].get("sort_seq") or 0 if rows else 0
        except Exception:
            return 0

    gui = None
    gui_lock = threading.Lock()
    send_lock = threading.RLock()
    dead_chats = set()
    fail_streak = {}
    send_retry_until = {}  # 窗口故障只短暂冷却，不能当成被移出群。
    self_wxid = db.wxid
    # 2026-10-27：把「我是谁」提到外层，payload 里要带上，
    # 模型才知道群里那个「琦」就是自己，不是别人。
    global SELF_WXID, SELF_NICK
    SELF_WXID = str(self_wxid or "")
    try:
        SELF_NICK = str(db.get_self_info().get("nick_name") or "")
    except Exception:
        SELF_NICK = ""
    log("自身身份：%s（%s）" % (SELF_NICK or "?", SELF_WXID or "?"))
    # 账号身份走结构化记录，不从已脱敏或历史日志猜测当前账号。
    import psutil
    identity_path=os.path.join(ROOT,"data","bridge-identity.json")
    identity={"pid":os.getpid(),"processStartedAt":psutil.Process().create_time(),
              "wxid":SELF_WXID,"nick":SELF_NICK,"verifiedAt":time.time()}
    with open(identity_path+".tmp","w",encoding="utf-8") as identity_file:
        json.dump(identity,identity_file,ensure_ascii=False)
        identity_file.flush();os.fsync(identity_file.fileno())
    os.replace(identity_path+".tmp",identity_path)
    pending_replies = {}
    SEND_STATE = {"active": False, "start": 0.0, "snapshot": None}
    pending_lock = threading.Lock()
    AWAY = {"on": False}
    NOTIFIED = []

    def away_active():
        """主人在微信里发了「我去玩了」→ 写 data/away.json，这里据此静默排队。"""
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "away.json")
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            return float(d.get("until") or 0) > time.time()
        except Exception:
            return False

    def game_running():
        if not GAME_TOKENS:
            return False
        try:
            import psutil
            for p in psutil.process_iter(["name"]):
                n = (p.info.get("name") or "").lower()
                if any(t in n for t in GAME_TOKENS):
                    return True
        except Exception:
            pass
        return False

    def foreground_is_fullscreen():
        """前台是不是「真全屏」程序（全屏游戏、全屏视频）——这时别抢焦点，先把回复排着。

        不能拿屏幕尺寸判断：Windows 上最大化窗口的 GetWindowRect 带 DWM 隐形边框，
        实测 2560x1440 的屏幕，最大化浏览器返回 rect=(-8,-8,2568,1448)，
        旧的 `r[2] >= sw-2 and r[3] >= sh-2` 会把它当全屏 ——
        表现就是「用户把窗口一最大化，她就不理人了」（3.3.1 实测复现）。

        改成两条一起判：
          ① 盖住整个显示器（MonitorFromWindow 取 rcMonitor，不是屏幕尺寸）；
          ② 没有标题栏和边框（真全屏游戏/无边框视频都是 WS_POPUP）。
        最大化窗口带 WS_CAPTION|WS_THICKFRAME，因此被正确排除。
        """
        try:
            import ctypes
            import ctypes.wintypes as wt
            import psutil
            import win32con
            import win32gui
            import win32process
            h = win32gui.GetForegroundWindow()
            if not h:
                return False
            _, pid = win32process.GetWindowThreadProcessId(h)
            name = psutil.Process(pid).name().lower()
            if name.startswith("weixin"):
                return False
            style = win32gui.GetWindowLong(h, win32con.GWL_STYLE)
            if (style & win32con.WS_CAPTION) and (style & win32con.WS_THICKFRAME):
                return False        # 普通窗口或最大化：有标题栏+可调边框，不是全屏

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                            ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

            user32 = ctypes.windll.user32
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            hmon = user32.MonitorFromWindow(h, 2)  # MONITOR_DEFAULTTONEAREST
            if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                return False
            mon = mi.rcMonitor
            r = win32gui.GetWindowRect(h)
            return (r[0] <= mon.left + 1 and r[1] <= mon.top + 1
                    and r[2] >= mon.right - 1 and r[3] >= mon.bottom - 1)
        except Exception:
            return False

    membership_misses = {}
    def group_is_alive(chat_username):
        """被踢出群后，群会话还在，但发不出去；先查成员列表。"""
        if not str(chat_username).endswith("@chatroom"):
            return True
        try:
            members = db.get_group_members(chat_username) or []
            ids = {str(m.get("wxid") or m.get("username")) for m in members}
            if not ids:
                return True  # 未加载成员列表是未知，不能永久标记退群。
            if self_wxid in ids:
                membership_misses.pop(chat_username, None)
                return True
            now = time.monotonic()
            first, count = membership_misses.get(chat_username, (now, 0))
            membership_misses[chat_username] = (first, count + 1)
            return not (count >= 2 and now-first >= 60)
        except Exception:
            return True

    # ---------- 微信窗口置顶（关键：别的窗口挡在微信上面会导致打字落空） ----------
    account_window_cache = {"checked": 0.0, "pids": set(), "birth": {}}

    def account_window_pids():
        """按现有账号文件归属绑定微信进程；不读取文件内容或任何新凭据。"""
        import psutil
        now = time.monotonic()
        cached = account_window_cache["pids"]
        if now - account_window_cache["checked"] < 60 and cached and all(psutil.pid_exists(p) and psutil.Process(p).create_time()==account_window_cache["birth"].get(p) for p in cached):
            return cached
        account = str((cfg.get("wechat") or {}).get("account") or "").strip().lower()
        if not account:
            return set()
        found = set()
        for process in psutil.process_iter(["name"]):
            if str(process.info.get("name") or "").lower() != "weixin.exe":
                continue
            try:
                if any(account in item.path.lower() for item in process.open_files()):
                    found.add(process.pid)
            except (psutil.Error, OSError):
                continue
        account_window_cache.update(checked=now, pids=found, birth={p:psutil.Process(p).create_time() for p in found})
        return found

    def create_wechat_gui():
        hwnd = find_weixin_hwnd()
        if not hwnd:
            raise RuntimeError("无法确认机器人账号对应的微信主窗，暂停发送以免操作其他账号")
        return WeChatGUI(hwnd=hwnd)

    def find_weixin_hwnd():
        try:
            import win32gui
            import win32con
            import win32process
            import psutil
        except Exception:
            return 0
        found = []
        permitted_pids = account_window_pids()
        if not permitted_pids:
            return 0
        # 微信（4.x / Qt）会额外建一个「托盘图标隐藏消息窗」：它算可见窗口，
        # 而且 GetWindowRect 出来的面积常常比主窗还大（实测 1320x900 > 主窗 896x776）。
        # 以前按「面积最大」挑，就会挑中它 —— 对它 SetWindowPos 直接 error(5) 拒绝访问，
        # 于是每次发送都失败、走 OCR 兜底、再被看门狗重启，一直循环报「拒绝访问」。
        BAD_CLASS = ("trayicon", "messagewindow", "msctf", "sopy", "sogou", "hintwnd", "default ime", "notifyicon")

        def cb(h, _):
            try:
                title = win32gui.GetWindowText(h) or ""
                _, pid = win32process.GetWindowThreadProcessId(h)
                if pid <= 0 or pid not in permitted_pids:
                    return
                cls = win32gui.GetClassName(h) or ""
                low = cls.lower()
                if any(bad in low for bad in BAD_CLASS):
                    return
                rect = win32gui.GetWindowRect(h)
                area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                if win32gui.IsIconic(h):
                    # 最小化后 GetWindowRect 是 (-16000,-16000,…) 的退化矩形，
                    # 换算成「还原后」的尺寸再判大小
                    try:
                        rc = win32gui.GetWindowPlacement(h)[4]
                        area = max(0, rc[2] - rc[0]) * max(0, rc[3] - rc[1]) or area
                    except Exception:
                        pass
                # 不再要求 IsWindowVisible：微信缩在托盘里时主窗 visible=0，以前这里
                # 直接 return，于是整条「置顶 + 调窗口」都被跳过。主窗至少 300x250，
                # 托盘提示窗（260x85）和辅助窗（173x196）顺手被排除。
                if area < 300 * 250 and title.strip() != "微信":
                    return
                try:
                    name = psutil.Process(pid).name().lower()
                except Exception:
                    return
                if not name.startswith("weixin"):
                    return
                if "发送" in title or "助手" in title:
                    return
                try:
                    style = win32gui.GetWindowLong(h, win32con.GWL_STYLE)
                    has_min_box = bool(style & win32con.WS_MINIMIZEBOX)
                except Exception:
                    has_min_box = False
                is_main_title = title.strip() == "微信"
                looks_main_class = ("qwindowicon" in low) or ("mmui::mainwindow" in low)
                # 2026-10-27 修（「发完不最小化」的真正根因）：
                #   微信 4.x 会同时存在两个标题都叫「微信」、类名都含 QWindowIcon 的窗口：
                #     hwnd=132788  真主窗，visible=1（最小化时仍是 1），UIA 挂的就是它
                #     hwnd=132750  「影子窗」，visible=0，永远不显示
                #   两者面积只差 1110（影子窗反而略大），所以以前按面积评分会选中影子窗。
                #   对 visible=0 的窗口调 ShowWindow(SW_MINIMIZE) 是空操作 ——
                #   于是「发完最小化」从来没生效过，窗口就一直摆在桌面上。
                #   主窗即使最小化/缩托盘时 visible 仍可能为 1，所以给可见窗口一个
                #   压倒面积的权重（10**6，远大于两者 1110 的面积差），
                #   但**不硬排除**不可见窗口 —— 真缩到托盘时两个都不可见，还能退回按面积挑。
                visible = bool(win32gui.IsWindowVisible(h))
                score = area
                if is_main_title:
                    score += 10 ** 9
                if looks_main_class:
                    score += 10 ** 8
                if has_min_box:
                    score += 10 ** 7
                if visible:
                    score += 10 ** 6
                found.append((score, 1 if is_main_title else 0, h))
            except Exception:
                pass

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return 0
        if not found:
            return 0
        found.sort(reverse=True)
        return found[0][2]

    def account_owns_hwnd(hwnd):
        """窗口动作前再核对账号归属，拒绝失效句柄、辅助窗及其他账号。"""
        try:
            import win32gui
            import win32process
            if not hwnd or not win32gui.IsWindow(hwnd):
                return False
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return (pid in account_window_pids()
                    and win32gui.GetWindowText(hwnd) == "微信"
                    and bool(win32gui.IsWindowEnabled(hwnd)))
        except Exception:
            return False

    def uia_weixin_hwnd():
        """只使用机器人账号已绑定的窗口，不全局搜索第一个微信主窗。"""
        candidates = []
        if gui is not None:
            candidates.append(getattr(gui, "main_hwnd", 0))
        engine = UIA_CACHE.get("u")
        if engine is not None:
            candidates.append(getattr(engine, "hwnd", 0))
        for hwnd in candidates:
            if account_owns_hwnd(hwnd):
                return int(hwnd)
        hwnd = find_weixin_hwnd()
        return int(hwnd) if account_owns_hwnd(hwnd) else 0

    def wechat_window_alive():
        """微信主窗现在是否还存在（每次现找，不缓存 —— 微信窗口回收/更新后，缓存句柄会失效）。

        长时间挂着不管时，微信可能半夜自更新、或把窗口拆了重开，此时 uiautomation
        手里还攥着旧窗口的 COM 指针 / 句柄，再碰一次就直接 0xC0000005（访问违规），
        Python 的 try/except 拦不住，进程当场死掉 —— 也就是「Bridge exited with code
        -1073741819」。这些 UIA 调用前先问一句窗口还在不在，就能把这种崩溃挡在外面。
        """
        try:
            import win32gui
            h = find_weixin_hwnd()
            return bool(h and win32gui.IsWindow(h))
        except Exception:
            return False

    def normalize_window(hwnd):
        """微信窗口太小 / 超出屏幕 / 被挪走都会让 UI 自动化点空，统一调成稳定尺寸。"""
        try:
            import win32gui
            import win32con
            import win32api
            sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
            rect = win32gui.GetWindowRect(hwnd)
            x, y, w, h = rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]
            tw, th = min(1320, sw - 160), min(900, sh - 160)
            need = w < 1080 or h < 760 or w > sw - 80 or h > sh - 80 or x < -20 or y < -20 or x + w > sw + 20 or y + h > sh + 20
            if need:
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 40, 20, tw, th, win32con.SWP_SHOWWINDOW)
                log("微信窗口调整：%dx%d @(%d,%d) → %dx%d（保证自动发送稳定）" % (w, h, x, y, tw, th))
                time.sleep(0.8)
        except Exception as e:
            log_window_problem("调整微信窗口失败（继续发送）", e)

    from contextlib import contextmanager
    @contextmanager
    def ui_operation():
        if not send_lock.acquire(timeout=3):
            raise TimeoutError("微信界面忙，本次任务延后")
        nested = bool(SEND_STATE["active"])
        try:
            if not nested:
                SEND_STATE.update(active=True, start=time.monotonic(), snapshot=None)
                SEND_STATE["snapshot"] = snapshot_win_state()
                try:
                    import faulthandler
                    faulthandler.dump_traceback_later(40, repeat=False, file=_hang_file)
                except Exception:
                    pass
            yield
        finally:
            try:
                if not nested:
                    restore_win_state(SEND_STATE.get("snapshot"), force_minimize=bool(BACKGROUND_SEND))
            finally:
                if not nested:
                    try:
                        import faulthandler
                        faulthandler.cancel_dump_traceback_later()
                    except Exception:
                        pass
                    SEND_STATE.update(active=False, snapshot=None)
                send_lock.release()

    def force_send_foreground(fn):
        """把微信拉到前台再发送（真正的兜底路径）。

        注意：实测下来大多数时候**不需要**这条路 —— 微信最小化、被别的窗口挡住、
        甚至前台是全屏程序时，UIA 都能正常发送（见 docs/接入微信.md 的实测数据）。
        所以它只作为「后台发送失败后」的第二次尝试。
        """
        hwnd = find_weixin_hwnd()
        was_topmost = False
        touched = False
        try:
            import ctypes
            import win32gui
            import win32con
            if hwnd:
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        # 微信在托盘里时主窗是隐藏的，先把窗口拉出来再打字
                        ctypes.windll.user32.ShowWindowAsync(hwnd, win32con.SW_SHOW)
                        ctypes.windll.user32.ShowWindowAsync(hwnd, win32con.SW_RESTORE)
                        log("微信在托盘里，已把主窗口拉出来再发送")
                        time.sleep(0.4)
                except Exception as e:
                    log("拉出微信窗口失败：", repr(e))
                normalize_window(hwnd)
                try:
                    fg = win32gui.GetForegroundWindow()
                except Exception:
                    fg = 0
                if fg != hwnd:
                    touched = True
                    force_foreground(hwnd)
                    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                    was_topmost = bool(ex & win32con.WS_EX_TOPMOST)
                    # 置顶失败不该让整条发送失败（比如挑到了不能定位的窗口），
                    # 顶多是“可能被别的窗口挡住”，打字还有 UIA 兜着
                    try:
                        win32gui.SetWindowPos(
                            hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | 0x4000,
                        )
                    except Exception as e:
                        log_window_problem("窗口置顶失败（继续发送）", e)
                    ctypes.windll.user32.ShowWindowAsync(hwnd, win32con.SW_RESTORE)
                    time.sleep(0.2)
            return fn()
        finally:
            try:
                if touched and hwnd and not was_topmost and account_owns_hwnd(hwnd):
                    import win32gui
                    import win32con
                    win32gui.SetWindowPos(
                        hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | 0x4000,
                    )
            except Exception:
                pass

    def with_window_on_top(fn):
        """兼容入口：统一生命周期，不重复调用已提交的发送函数。"""
        with ui_operation():
            return fn()

    def force_foreground(hwnd):
        """不共享输入队列，避免自动化卡住时拖住用户的鼠标操作。"""
        try:
            import ctypes
            if not account_owns_hwnd(hwnd):
                return False
            user32 = ctypes.windll.user32
            user32.ShowWindowAsync(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            return user32.GetForegroundWindow() == hwnd
        except Exception:
            return False

    UIA_CACHE = {"u": None, "checked": 0.0}

    def _input_value(u):
        """尽量读出输入框当前文字；读不到返回空串。"""
        try:
            e = u._chat_input()
            if e is None:
                return ''
            for name in ('get_value', 'value'):
                try:
                    v = getattr(e, name)
                    v = v() if callable(v) else v
                    if v is not None:
                        return str(v)
                except Exception:
                    pass
            try:
                return str(e.iface_value.Value)
            except Exception:
                pass
        except Exception:
            pass
        return ''

    def press_enter(with_ctrl=False):
        """用底层按键补发 Enter（UIA 的 SendKeys 有时不触发发送）。"""
        try:
            import win32api
            import win32con
            hwnd = find_weixin_hwnd()
            if hwnd:
                force_foreground(hwnd)
            time.sleep(0.10)
            if with_ctrl:
                win32api.keybd_event(0x11, 0, 0, 0)
            win32api.keybd_event(0x0D, 0, 0, 0)
            win32api.keybd_event(0x0D, 0, win32con.KEYEVENTF_KEYUP, 0)
            if with_ctrl:
                win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.25)
            return True
        except Exception as e:
            log("手动回车失败：", repr(e))
            return False

    def snapshot_win_state():
        """记下发送前的「窗口长什么样」，发完要还原。

        要还原的东西：微信是不是最小化、原来哪个窗口在前台、
        以及窗口原来的位置和大小（normalize_window 会把它改成 1320x900）。
        """
        snap = {"hwnd": 0, "iconic": False, "fg": 0, "rect": None}
        try:
            import win32gui
            # 先问 UIA：它绑的那个才是引擎真正在操作的窗口。
            # 微信 4.x 有两个同名「微信」顶层窗，win32 枚举可能挑中 visible=0 的影子窗，
            # 对它 ShowWindow 是空操作 —— 这正是「发完不最小化」的根因。
            snap["hwnd"] = uia_weixin_hwnd() or 0
            if snap["hwnd"]:
                snap["iconic"] = bool(win32gui.IsIconic(snap["hwnd"]))
                snap["visible"] = bool(win32gui.IsWindowVisible(snap["hwnd"]))
                snap["topmost"] = bool(win32gui.GetWindowLong(snap["hwnd"], -20) & 8)
                # 2026-10-27 加：以前不记尺寸，normalize_window 把窗口改成 1320x900 后
                # 就一直那样了 —— 用户「调出来用完后要放回去」的要求没法满足。
                if not snap["iconic"]:
                    snap["rect"] = tuple(win32gui.GetWindowRect(snap["hwnd"]))
            snap["fg"] = win32gui.GetForegroundWindow() or 0
        except Exception:
            pass
        return snap

    def restore_win_state(snap, force_minimize=False):
        """成功、失败、超时均收尾；只操作账号归属仍然有效的原窗口。"""
        from window_cleanup import cleanup_window
        try:
            result = cleanup_window(snap, account_owns_hwnd, minimize=force_minimize)
            if not result:
                log("窗口收尾跳过：原窗口不存在或账号归属无法确认")
        except Exception as error:
            log_window_problem("窗口收尾失败", error)

    # 2026-10-27 加（主人：「每次查看图片后不会叉掉，不久会堆满桌面」）：
    #   看原图必须点开图片气泡，微信会弹一个独立的「图片和视频」预览窗。
    #   wechatauto 的 download_image_original() 只负责点开、从不关掉 ——
    #   它自己的日志里就写着「屏幕上已有 N 个预览窗：本轮只把新出现的窗口算作点开成功」，
    #   那就是上一张留下的。堆久了桌面全是看图窗，还会让下一张图的判据变脏。
    #   这里统一收口：看图前后各点一次名，只关「这一轮新冒出来的」，
    #   用户自己手动开着的预览窗不碰。
    _PREVIEW_TITLES = ("图片和视频",)
    WM_CLOSE = 0x0010

    def _preview_hwnds():
        """当前所有图片预览窗的句柄。

        微信 4.x 的看图窗有两种形状（实测）：
          ① 顶层就是 mmui::PreviewWindow，Name 是 'Weixin'；
          ② 顶层是 Qt...QWindowIcon、标题「图片和视频」，里面才是 PreviewWindow。
        两种都算，别的窗口一律不动。
        """
        out = []
        try:
            import win32gui

            def _cb(h, _lparam):
                try:
                    cls = win32gui.GetClassName(h) or ""
                    title = (win32gui.GetWindowText(h) or "").strip()
                    if "PreviewWindow" in cls or (title in _PREVIEW_TITLES
                                                  and cls.startswith("Qt")):
                        out.append(int(h))
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass
        return out

    def _save_dialog_hwnds(pids=()):
        """微信弹的「保存」另存为对话框（Windows 通用 #32770）。

        这类对话框是**模态**的：它开着的时候微信主窗被禁用，发送通道整条瘫掉。
        所以关窗时要优先把它干掉，而且不能等预览窗先关 —— 模态框不关，
        预览窗的消息循环就是堵的，PostMessage 也处理不了。
        """
        out = []
        try:
            import win32gui
            import win32process

            def _cb(h, _lparam):
                try:
                    if (win32gui.GetClassName(h) or "") != "#32770":
                        return True
                    if not win32gui.IsWindowVisible(h):
                        return True
                    if pids:
                        _tid, pid = win32process.GetWindowThreadProcessId(h)
                        if pid not in pids:
                            return True
                    title = (win32gui.GetWindowText(h) or "").strip()
                    # 标题是「保存」/「另存为」；不确定时也不乱关别的程序的对话框，
                    # 靠 pid 过滤兜住。
                    if title in ("保存", "另存为") or "保存" in title:
                        out.append(int(h))
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass
        return out

    def close_image_previews(keep=()):
        """关掉这一轮点开的看图窗，返回关掉了几个。

        keep 里的句柄是「看图之前就已经开着的」，那是主人自己开的，不动。
        只关属于机器人账号那个微信进程的窗口，别误伤别的号或别的程序。
        """
        keep = set(int(h) for h in (keep or ()))
        try:
            import win32gui
            import win32process
        except Exception:
            return 0
        pids = account_window_pids()
        if not pids:
            # 认不出哪个进程属于机器人账号时**不关**：这台机器上可能同时开着
            # 主号和小号的微信，关错就是把主人正在看的图给叉了。
            log("认不出机器人账号的微信进程，本轮不关看图窗（避免误关其他号）")
            return 0
        # 先关模态的另存为对话框：它不关，预览窗的 PostMessage 处理不了，
        # 微信主窗也一直处于禁用状态（发送会报「微信主窗被弹窗禁用」）。
        for h in _save_dialog_hwnds(pids):
            try:
                win32gui.PostMessage(h, WM_CLOSE, 0, 0)
                time.sleep(0.3)
                if win32gui.IsWindow(h):
                    # 模态框可能不响应 WM_CLOSE，再按一次 ESC
                    try:
                        import win32api
                        import win32con as _wc
                        win32api.keybd_event(_wc.VK_ESCAPE, 0, 0, 0)
                        win32api.keybd_event(_wc.VK_ESCAPE, 0, 0,
                                             _wc.KEYEVENTF_KEYUP)
                        time.sleep(0.3)
                    except Exception:
                        pass
                if win32gui.IsWindow(h):
                    log("另存为对话框没关掉：hwnd=%d（发送可能被它堵住）" % h)
                else:
                    log("已关掉另存为对话框 hwnd=%d" % h)
            except Exception as e:
                log("关闭另存为对话框失败：hwnd=%d %r" % (h, e))
        closed = 0
        for h in _preview_hwnds():
            if h in keep or not win32gui.IsWindow(h):
                continue
            try:
                _tid, pid = win32process.GetWindowThreadProcessId(h)
                if pid not in pids:
                    continue                      # 不是机器人这个微信进程的窗口
                win32gui.PostMessage(h, WM_CLOSE, 0, 0)
            except Exception as e:
                log("关闭看图窗失败：hwnd=%d %r" % (h, e))
                continue
            # 给它一点时间消失；关不掉再补一次 UIA 的 WindowPattern.Close
            for _ in range(6):
                time.sleep(0.12)
                if not win32gui.IsWindow(h):
                    break
            if win32gui.IsWindow(h):
                try:
                    import uiautomation as _auto
                    _w = _auto.ControlFromHandle(h)
                    if _w is not None:
                        _w.GetWindowPattern().Close(0.2)
                except Exception:
                    pass
                time.sleep(0.2)
            if win32gui.IsWindow(h):
                log("看图窗没关掉（可能被别的操作卡住）：hwnd=%d" % h)
            else:
                closed += 1
        if closed:
            log("已关掉 %d 个看图窗" % closed)
        return closed

    def install_quiet_uia(u):
        """让 UIA 引擎「别抢前台」。

        wechatauto 的 WeChatUIA.ensure_window() 每次都会调 _activate() →
        _force_foreground()/ShowWindow(9)，也就是把微信窗口恢复并抢到最前面。
        实测 18/18：微信最小化、被遮挡、前台全屏时其实都能发，所以这步纯属多余，
        代价是你正在用的电脑被反复打断。

        这里给引擎实例打个补丁：quiet_mode 打开时 _activate 直接返回。
        不改第三方库文件，随桥接一起走；万一后台发不出去，force_send_foreground()
        会显式把窗口拉出来，不依赖这个补丁。
        """
        try:
            import types
            if getattr(u, "_whale_quiet_patch", False):
                u.quiet_mode = True
                return
            orig = u._activate

            def patched(self, win, _orig=orig):
                if getattr(self, "quiet_mode", False):
                    return
                return _orig(win)

            u._activate = types.MethodType(patched, u)
            u._whale_quiet_patch = True
            u.quiet_mode = True
        except Exception as e:                      # noqa: BLE001
            log("后台模式补丁没打上（不影响发送）：", repr(e))

    def uia_send(text, who, username=""):
        """优先用 UIA 发送：不依赖 OCR 找发送按钮，成功后返回 True。"""
        if not wechat_window_alive():
            # 主窗没了，缓存引擎里的句柄已失效，碰了会 0xC0000005；直接走键盘/OCR 兜底
            with gui_lock:
                UIA_CACHE["u"] = None
            return False
        u = UIA_CACHE["u"]
        # 每次发送都问一次库里的引擎（它内部 30 秒节流、会自己检测「子控件不可见」并重新热激活）。
        # 以前只在缓存为空时才问，结果微信把 accessibility gate byte 归零后，缓存的引擎变成
        # 「假死」：能收消息、但一条也发不出去，挂着挂着就不理人了，只能重启桥接才好。
        try:
            with gui_lock:
                fresh = gui._get_uia() if gui is not None else None
            if fresh is not None:
                u = fresh
            UIA_CACHE["u"] = u
        except Exception:
            u = UIA_CACHE["u"]
        if u is None:
            # 同上：节流窗口内缓存是 None 时，强制重激活一次再放弃。
            # 不带 refresh 的话，这次调用只会把缓存里的 None 原样还回来。
            if recover_uia("uia_send 前 UIA 引擎不可用"):
                u = UIA_CACHE.get("u")
        if u is None:
            return False
        if BACKGROUND_SEND:
            # 后台模式：禁止引擎自己恢复窗口/抢前台（真正的抢前台只在兜底那一步做）
            install_quiet_uia(u)
        keys = aliases_for(username, who) if username else [who]
        try:
            cur = None
            try:
                cur = u.current_chat()
            except Exception:
                pass
            hit_cur = (cur == who) or any(cur == k for k in keys if k)
            if not hit_cur and username:
                hit_cur = bool(cur) and open_cache.get(username) == cur
            if not hit_cur:
                if not u.open_chat(keys):
                    try:
                        u.back_to_chat_tab()
                    except Exception:
                        pass
                    if not u.open_chat(keys):
                        return False
                if username:
                    try:
                        open_cache[username] = u.current_chat() or ""
                        log("UIA 已切到会话：", open_cache[username])
                    except Exception:
                        pass
            t_open = time.time()
            sent_ok = False
            try:
                sent_ok = bool(u.send_text(text))
            except Exception:
                sent_ok = False
            if not sent_ok:
                try:
                    u.back_to_chat_tab()
                    sent_ok = bool(u.send_text(text))
                except Exception:
                    sent_ok = False
            log("UIA 发送耗时 %.1fs（打开会话+输入）" % (time.time() - t_open))
            if sent_ok:
                # 多按几次：Enter 两次 + Ctrl+Enter 两次（兼容“按 Ctrl+Enter 发送”的设置）
                press_enter(False)
                press_enter(False)
                press_enter(True)
                press_enter(True)
                return True
        except Exception as e:
            log("UIA 发送异常：", repr(e))
        return False

    from wechat_window_recovery import WindowRecovery
    window_recovery = WindowRecovery(ROOT, log)

    def recover_wechat_window(reason):
        # 发送过程以外的心跳恢复不能和发送线程争抢窗口。
        blocked = bool(game_running() or foreground_is_fullscreen() or SEND_STATE["active"])
        if window_recovery.recover(reason, find_weixin_hwnd(), blocked=blocked):
            with gui_lock:
                UIA_CACHE["u"] = None
            return True
        return False

    def recover_uia(reason=""):
        """强制重新热激活 UIA（rewrite gate byte + 校验），成功返回 True。

        微信跑久了会把 accessibility gate byte 归零，UIA 引擎就变成「假死」：
        能读消息、发不出去。以前只能重启桥接；这里让它自己救回来。
        """
        try:
            if gui is None:
                return False
            if not wechat_window_alive():
                with gui_lock:
                    UIA_CACHE["u"] = None
                log("UIA 重新热激活跳过（微信主窗当前不存在）")
                return False
            with gui_lock:
                u = gui._get_uia(refresh=True)
            UIA_CACHE["u"] = u
            if u is not None:
                log("UIA 已重新热激活%s" % (("（" + reason + "）") if reason else ""))
                return True
            log("UIA 重新热激活失败%s" % (("（" + reason + "）") if reason else ""))
        except Exception as e:                     # noqa: BLE001
            log("UIA 重新热激活异常：", repr(e))
        return False

    def keyboard_send(text, chat_display_name, chat_username=""):
        """完全不依赖 UIA 的发送通道：Ctrl+F 搜索会话 → 粘贴 → 回车 → 回读数据库确认。

        为什么需要它：UIA 依赖微信的 accessibility gate，微信跑久了/窗口状态怪的时候
        整条路都会挂（见 recover_uia 的注释）。键盘通道只用 Win32 按键 + 剪贴板，
        走的是微信自己的搜索框，不碰 accessibility。
        代价：会抢前台、慢一些，所以只在 UIA 真的发不出去时用。
        """
        try:
            import ctypes
            import win32api          # noqa: F401
            import win32clipboard
            import win32con
            import win32gui
            user32 = ctypes.windll.user32
        except Exception as e:                     # noqa: BLE001
            log("键盘通道不可用（缺少 win32 模块）：", repr(e))
            return False

        hwnd = find_weixin_hwnd()
        if not hwnd:
            log("键盘通道：找不到微信主窗")
            return False

        def fg():
            return user32.GetForegroundWindow()

        # 抢前台：单独 SetForegroundWindow 会被前台锁拒绝，先按一下 ALT 解锁
        if fg() != hwnd:
            user32.keybd_event(win32con.VK_MENU, 0, 0, 0)
            time.sleep(0.05)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.2)
            user32.keybd_event(win32con.VK_MENU, 0, 2, 0)
            time.sleep(0.2)
        if fg() != hwnd:
            try:
                user32.SwitchToThisWindow(hwnd, True)
                time.sleep(0.3)
            except Exception:
                pass
        if fg() != hwnd:
            log("键盘通道：抢不到前台，放弃这次（UIA 也不可用时这条就发不出去了）")
            return False

        def set_clip(t):
            for _ in range(5):
                try:
                    win32clipboard.OpenClipboard()
                    break
                except Exception:                  # noqa: BLE001
                    time.sleep(0.1)
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, t)
            finally:
                win32clipboard.CloseClipboard()

        def tap(vk, mods=()):
            for m in mods:
                user32.keybd_event(m, 0, 0, 0)
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, 2, 0)
            for m in reversed(mods):
                user32.keybd_event(m, 0, 2, 0)
            time.sleep(0.12)

        def confirmed_target():
            """打开会话后确认「现在开着的到底是谁」。

            键盘通道是模拟按键，搜不到人时回车会落到「当前那个会话」上，
            正文就直接发给别人了 —— 2026-10-25 12:21 的日志里就出现过
            「键盘通道：按键发了，但数据库里没确认到（可能发到了别的会话）」。
            这里用 UIA 的只读能力核对（读会话名在「能读不能发」的假死状态下依然可用）；
            读不到就宁可这条失败，也不盲发 —— 私聊内容发错人比晚回一条严重得多。
            返回 (是否匹配, 当前会话名)。
            """
            try:
                u = gui._get_uia() if gui is not None else None
                cur = u.current_chat() if u is not None else None
            except Exception:
                cur = None
            if not cur:
                return None, None
            cands = {chat_display_name}
            try:
                if chat_username:
                    cands.update(aliases_for(chat_username, chat_display_name))
                    cached = open_cache.get(chat_username)
                    if cached:
                        cands.add(cached)
            except Exception:
                pass
            return (cur in cands), cur

        try:
            # ① 搜索会话（微信的 Ctrl+F）
            tap(ord("F"), (win32con.VK_CONTROL,))
            time.sleep(0.5)
            set_clip(chat_display_name)
            tap(ord("V"), (win32con.VK_CONTROL,))
            time.sleep(0.8)
            tap(win32con.VK_RETURN)          # ② 打开会话
            time.sleep(0.9)
            # ②.5 先核对打开的是不是目标会话，确认不了就不往输入框里打字
            matched, cur = confirmed_target()
            if matched is None:
                log("键盘通道：读不到当前会话名，为防发错人这次不发")
                return False
            if not matched:
                log("键盘通道：打开的是「%s」而不是「%s」，为防发错人这次不发"
                    % (cur or "", chat_display_name))
                return False
            set_clip(text)                    # ③ 粘贴正文
            tap(ord("V"), (win32con.VK_CONTROL,))
            time.sleep(0.4)
            tap(win32con.VK_RETURN)          # ④ 发送
        except Exception as e:                 # noqa: BLE001
            log("键盘通道按键失败：", repr(e))
            return False

        # ⑤ 回读数据库确认这条真的发出去了（搜错会话时能立刻发现）
        for _ in range(8):
            time.sleep(0.5)
            try:
                msgs = db.get_messages(chat_username or chat_display_name, limit=3) or []
            except Exception:
                msgs = []
            for m in msgs:
                if str(m.get("content") or "").strip()[:12] == str(text).strip()[:12]:
                    return True
        log("键盘通道：按键发了，但数据库里没确认到（可能发到了别的会话），这条按失败处理")
        return False

    from exact_group_sender import send_exact_group
    from progress_reply import wait_for_response
    progress_last_sent = {}
    from delivery_journal import DeliveryJournal
    delivery_journal = DeliveryJournal(os.path.join(ROOT, "data", "wechat-delivery-journal.json"))
    task_context = threading.local()
    from ui_executor import UiExecutor
    ui_executor = UiExecutor()

    def send_group_checked(text, group_id):
        task_id=getattr(task_context,"id","")
        def run():
            task_context.id=task_id
            return _send_group_checked_impl(text,group_id)
        return ui_executor.call(run)

    def _send_group_checked_impl(text, group_id):
        nonlocal gui
        with ui_operation():
            try:
                import win32gui
                hwnd = find_weixin_hwnd()
                if hwnd and not win32gui.IsWindowEnabled(hwnd):
                    send_retry_until[group_id] = time.monotonic() + 5
                    log("群消息未发送：微信主窗被弹窗禁用")
                    return False
                if not wechat_window_alive():
                    UIA_CACHE["u"] = None
                    gui = None
                    return False
                if hwnd and account_owns_hwnd(hwnd):
                    import ctypes
                    if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                        ctypes.windll.user32.ShowWindowAsync(hwnd,9)
                        time.sleep(.3)
                with gui_lock:
                    if gui is None or getattr(gui,"main_hwnd",hwnd)!=hwnd:
                        gui = create_wechat_gui()
                        UIA_CACHE["u"] = None
                    u = gui._get_uia()
                if u is None:
                    # 2026-10-28 00:38 事故：她生成了「在」，却一条都没发出去。
                    # 原因不是微信坏了，是库里的 _get_uia() 有 30 秒节流 ——
                    # 这 30 秒内一律返回缓存值，缓存是 None 就一直是 None。
                    # 她正好撞进这个窗口，于是消息被直接丢掉：不排队、不重试，
                    # 主人那边只看到「不回我话了」。实际 00:39:02 UIA 自己就恢复了。
                    # 现在先强制重激活一次（refresh=True 绕过节流、重建引擎）再决定发不发。
                    if recover_uia("发送前 UIA 引擎不可用"):
                        with gui_lock:
                            u = UIA_CACHE.get("u")
                UIA_CACHE["u"] = u
                UIA_CACHE["checked"] = time.monotonic()
                if u is None:
                    log("群消息未发送：无法校验当前群，暂缓发送")
                    return False
                result_box = {}
                def submit_once():
                    result = send_exact_group(u, db, group_id, text, self_wxid, journal=delivery_journal, task_id=getattr(task_context, "id", ""))
                    result_box["result"] = result
                    return result.confirmed
                # 只尝试一次：提交结果未知时不能切换通道再次投递。
                force_send_foreground(submit_once)
                result = result_box.get("result")
                if result is not None and result.confirmed:
                    fail_streak.pop(group_id, None)
                    send_retry_until.pop(group_id, None)
                    log("群消息发送成功（原群 ID、本人发送、正文均已回读确认）")
                    return True
                send_retry_until[group_id] = time.monotonic() + 30
                log("群消息未确认，停止本次投递：", result.reason if result else "未提交")
                return False
            except Exception as exc:
                send_retry_until[group_id] = time.monotonic() + 30
                log("群发送校验异常，暂缓发送：", type(exc).__name__)
                return False

    def send_text(text, chat_display_name, chat_username="", force=False, quote_text=None, task_id=None):
        nonlocal gui
        import uuid
        task_context.id = task_id or uuid.uuid4().hex
        # 2026-10-27 加：所有发送的唯一出口，最后一道白名单闸门。
        #   上面 recent_chats() 已经过滤过，但离开通知 / 游戏通知 / 定时任务
        #   这些分支是直接拿名字发的，这里再拦一次才不会漏到别的群。
        if chat_username and not _chat_permitted(chat_username, chat_display_name):
            log("拒绝发送：%s 不在白名单里，跳过" % (chat_display_name or chat_username))
            return False
        if chat_username and chat_username in dead_chats:
            log("跳过已失效的会话：", chat_display_name)
            return False
        if chat_username and time.monotonic() < send_retry_until.get(chat_username, 0):
            log("发送通道仍在短暂冷却，稍后收到新消息会再次尝试")
            return False
        if chat_username and not group_is_alive(chat_username):
            dead_chats.add(chat_username)
            log("群里已经没有我了（被移出？），以后跳过：", chat_display_name)
            return False
        if not force and (AWAY["on"] or away_active() or game_running() or foreground_is_fullscreen()):
            with pending_lock:
                pending_replies[(chat_username, getattr(task_context,"id","") or text)] = (text, chat_display_name, chat_username, time.time(), getattr(task_context,"id",""))
            log("你在游戏/全屏程序里，回复先排队，等你退出再发（尚未确认送达）")
            return False
        if str(chat_username).endswith("@chatroom"):
            return send_group_checked(text, chat_username)
        # 当前用户要求只在群内回复，任何缺失群 ID 的出口都不能落到私聊。
        log("拒绝非群发送目标")
        return False

    def send_at(member, text, chat_display_name, chat_username="", task_id=None):
        """群内点名也走唯一精确群发送出口，避免成员选择误入私聊。"""
        if not member or not text:
            return False
        return send_text("@%s %s" % (member, text), chat_display_name, chat_username, task_id=task_id)

    def notify_master_fault(kind, detail=""):
        """出重要故障：在群里 @ 主人 3 次，然后宣布静默等他回话。

        主人 2026-10-27 原话：「遇到重要问题或者bug在群里@我,连续三次然后宣布自己静默直到我回复」

        为什么要连发 3 次：微信 @ 有时不弹提醒（他在忙/手机静音），连发三次更容易被看见。
        为什么发完要静默：故障往往每来一条消息就触发一次，不静默就会刷屏把他烦死。

        返回 True 表示真的报警了（调用方据此知道已进入静默期）。
        """
        # 防抖先判：两条故障别在同一秒炸出来。
        # 2026-10-27 修：以前先调 fault_note（会 +count）再判防抖，
        # 被防抖挡下的那次白白消耗了计数，日志上看着「报了 5 次」其实一条没发。
        now = time.time()
        if now - FAULT_STATE.get("last_notify", 0) < FAULT_NOTIFY_GAP:
            log("[故障] %s 在防抖窗口内，先攒着：%s" % (kind, str(detail)[:60]))
            return False
        # 挑报警的群：白名单群优先，没有就退到「最近活跃的白名单会话」。
        # 2026-10-27 修：这一步必须放在 fault_note 之前 —— 以前先计数再找群，
        # 找不到群时 return 了，可计数已经被吃掉；等群恢复正常时这一轮就被白白耗掉，
        # 主人反而收不到报警。
        chat_user, chat_name = "", ""
        try:
            _targets = recent_chats(limit=3, within=24 * 3600)
            for _u, _n in _targets:
                if str(_u).endswith("@chatroom"):
                    chat_user, chat_name = _u, _n
                    break
            if not chat_user and _targets:
                chat_user, chat_name = _targets[0]
        except Exception as e:
            log("报警：找不到白名单群 %r" % (e,))
        if not chat_user:
            log("[!] 有重要故障但找不到白名单群，没法 @ 主人：%s %s" % (kind, str(detail)[:80]))
            return False

        if not fault_note(kind, detail):
            return False

        who = ""
        try:
            who = display_name(MASTER_WXID) or ""
        except Exception:
            who = ""
        who = who or "主人"
        reason = FAULT_IMPORTANT.get(kind, kind)
        extra = ("（%s）" % str(detail)[:80]) if detail else ""
        body = "主人，出问题了：%s%s。我先 @ 你三次，然后闭嘴等你回话。" % (reason, extra)

        with FAULT_LOCK:
            FAULT_STATE["last_notify"] = now
            FAULT_STATE["silenced"] = True
            FAULT_STATE["silenced_since"] = now
            FAULT_STATE["silence_chat"] = chat_user
            FAULT_STATE["silence_name"] = chat_name
            for it in FAULT_STATE["items"].values():
                it["silenced"] = True

        log("[!] 重要故障，群里 @ 主人 3 次 → %s：%s" % (chat_name, reason))

        def _alarm():
            """三次 @ 单独跑线程：发消息慢（每次 3~30 秒），不能卡住消息处理。"""
            sent = 0
            for i in range(3):
                try:
                    ok = send_at(who, body, chat_name, chat_user)
                    if not ok:
                        ok = send_text("@%s %s" % (who, body), chat_name, chat_user, force=True)
                except Exception as e:            # noqa: BLE001
                    log("报警第 %d 次发送异常：%r" % (i + 1, e))
                    ok = False
                if ok:
                    sent += 1
                if i < 2:
                    time.sleep(2.5)
            # 收尾：宣布静默（这条也 @ 他，保证他翻记录时一眼看到）
            tail = "（已经 @ 你 %d 次了。我先静默，你回一句我再继续干活。）" % sent
            try:
                if not send_at(who, tail, chat_name, chat_user):
                    send_text("@%s %s" % (who, tail), chat_name, chat_user, force=True)
            except Exception as e:                # noqa: BLE001
                log("宣布静默失败：%r" % (e,))
            log("[!] 已 @ 主人 %d 次并宣布静默，等他在群里回话" % sent)

        threading.Thread(target=_alarm, daemon=True, name="fault-alarm").start()
        return True

    def _describe_image_impl(msg):
        try:
            import base64
            user = msg.get("username") or ""
            lid = msg.get("local_id")
            shard = msg.get("shard")
            if not user or lid is None:
                return ""
            # 先用完整图（原图 > 压缩版），别拿缩略图去识别；两档都没有才退回 best
            #
            # 2026-10-27 修（看图全是白板）：
            #   以前只用 download_image()，它只读本机缓存。可微信 4.x 的 .dat
            #   经常只是「预览档」—— 解密出来是个 wxgf 容器，里面只有 VPS/SPS/PPS
            #   参数集，没有图像切片，ffmpeg 硬转就是一张 18KB 的白图。
            #   实测：.dat 69803 B → 白图 18634 B；驱动界面下原图 → 1.6MB 真图。
            #   所以先试 download_image_original()（会点开气泡等 _h.dat 落盘），
            #   失败再退回只读本机的老路子。
            path = None
            # 下载原图要点开气泡、滚到那张图，会动微信窗口。
            # 和发送走同一套「先记下窗口状态 → 做完立刻还原」，
            # 用完就把微信放回去（该最小化的重新最小化，前台还给原来的窗口）。
            _snap = snapshot_win_state() if BACKGROUND_SEND else None
            # 2026-10-27 加：记下「看图之前就开着的」预览窗，收尾时只关这一轮新开的。
            _pre_views = set(_preview_hwnds())
            try:
                path = _get_media().download_image_original(
                    user, int(lid), chat_name=display_name(user),
                    timeout=40.0, scroll=True, shard=shard,
                )
                if path:
                    log("原图下载成功：%d KB" % (os.path.getsize(path) // 1024))
            except Exception as _e:
                log("原图下载失败（退回本机缓存）：%r" % (_e,))
                path = None
            finally:
                # 2026-10-27 加：先关看图窗，再还原主窗。
                #   顺序不能反 —— 预览窗是独立顶层窗，最小化主窗带不走它，
                #   先最小化主窗会让预览窗失去父窗口变成孤儿窗留在桌面上。
                #   失败路径也照样关：这一轮点开的窗，不管取没取到图都不该留着。
                close_image_previews(keep=_pre_views)
                # 下载成功了就顺手把微信最小化（主人要「用完放回去」）；
                # 没下到图就保持可见，别在可能重试的时候把窗口藏起来。
                if _snap:
                    restore_win_state(_snap, force_minimize=bool(path))
            if not path:
                for _tier in ("full", "best"):
                    try:
                        path = _get_media().download_image(user, int(lid), tier=_tier, shard=shard)
                    except Exception as _e:
                        log("图片下载失败（tier=%s）：%r" % (_tier, _e))
                        path = None
                    if path:
                        log("图片档位：%s → %d KB" % (_tier, os.path.getsize(path) // 1024))
                        break
            if not path:
                log("图片取不到本地文件")
                return ""
            with open(path, "rb") as f:
                raw = f.read()
            if len(raw) > 8 * 1024 * 1024:
                log("图片太大，跳过")
                return ""
            # 关键：按文件头判断真实格式。微信 4.x 的普通图片是 wxgf(HEVC) 容器，
            # 老代码只看扩展名，于是把 wxgf 原样当 image/jpeg 发出去，
            # 视觉接口就回「图片输入格式/解析错误」——表现就是「有时能认、有时不能」。
            if raw[:4] == b"wxgf":
                try:
                    conv = _get_media()._wxgf_to_jpg(raw)
                except Exception as _e:
                    conv = None
                    log("wxgf 转码异常：", repr(_e))
                if not conv:
                    log("这张图是 wxgf/HEVC，本机 ffmpeg 没能转出来，跳过（装 ffmpeg 到 C:\\ffmpeg 即可）")
                    return ""
                raw = conv
                ext = ".jpg"
            elif raw[:8] == b"\x89PNG\r\n\x1a\n":
                ext = ".png"
            elif raw[:3] == b"\xff\xd8\xff":
                ext = ".jpg"
            elif raw[:3] == b"GIF":
                ext = ".gif"
            else:
                ext = os.path.splitext(path)[1].lower()
                if ext not in (".jpg", ".jpeg", ".png", ".gif"):
                    log("图片格式不认识（%s），跳过" % raw[:4])
                    return ""
            mime = "image/png" if ext == ".png" else ("image/gif" if ext == ".gif" else "image/jpeg")
            base = bot_url.rsplit("/webhook", 1)[0]

            # 长截图（竖着很长）整张丢给模型，字会被缩糊；切成 2~3 段分别识别再拼起来
            parts = [raw]
            try:
                from PIL import Image
                import io as _io
                img = Image.open(_io.BytesIO(raw))
                img.load()
                w, h = img.size
                if w > 0 and h / float(w) > 2.2 and h > 1200:
                    n = 3 if h / float(w) > 4.0 else 2
                    overlap = int(h * 0.08)
                    parts = []
                    for i in range(n):
                        top = max(0, int(i * h / n) - (overlap if i else 0))
                        bottom = min(h, int((i + 1) * h / n) + overlap)
                        seg = img.crop((0, top, w, bottom)).convert("RGB")
                        buf = _io.BytesIO()
                        seg.save(buf, format="JPEG", quality=88)
                        parts.append(buf.getvalue())
                    log("长截图 %dx%d，切成 %d 段分别识别" % (w, h, len(parts)))
                else:
                    # 普通图：统一转 RGB JPEG 再发，规避 webp/heic 之类的格式坑
                    img2 = img.convert("RGB")
                    buf = _io.BytesIO()
                    img2.save(buf, format="JPEG", quality=88)
                    parts = [buf.getvalue()]
                    mime = "image/jpeg"
            except Exception as _e:
                log("图片预处理跳过（用原图）：", repr(_e))
                parts = [raw]

            texts = []
            for idx, part in enumerate(parts):
                prompt = ""
                if len(parts) > 1:
                    prompt = "这是一张长截图从上往下的第 %d/%d 段。逐条转写这一段里的文字（分清左边对方/右边本人），不要省略。" % (idx + 1, len(parts))
                r = requests.post(
                    base + "/api/vision",
                    json={"image": base64.b64encode(part).decode(), "mime": mime, "prompt": prompt},
                    timeout=120,
                )
                data = r.json()
                piece = str(data.get("text") or "").strip()
                if piece:
                    texts.append(piece)
                if not data.get("ok", True):
                    log("第 %d 段识别失败：%s" % (idx + 1, data.get("error")))
            text = "\n".join(texts).strip()
            if len(text) > 1200:
                text = text[:1200] + "…"
            log("图片识别：", text[:80])
            return text
        except Exception as e:
            log("图片识别失败：", repr(e))
            return ""

    def describe_image(msg):
        def run():
            with ui_operation():
                return _describe_image_impl(msg)
        return ui_executor.call(run)

    def transcribe_voice(msg):
        """语音消息 → 文字；失败返回空串。"""
        if not VOICE_ENABLED:
            log("语音识别已关闭（voice.enabled=false）")
            return ""
        try:
            import io
            import wave
            user = msg.get("username") or ""
            lid = msg.get("local_id")
            shard = msg.get("shard")
            if not user or lid is None:
                return ""
            path = _get_media().download_voice(user, int(lid), shard=shard)
            if not path:
                log("语音没落盘（这条微信没缓存到本地），跳过")
                return ""
            try:
                import pysilk
            except Exception:
                log("缺少 pysilk，装一下：pip install silk-python")
                return ""
            pcm = io.BytesIO()
            with open(path, "rb") as f:
                pysilk.decode(f, pcm, VOICE_SAMPLE_RATE)
            raw = pcm.getvalue()
            seconds = len(raw) / float(VOICE_SAMPLE_RATE * 2)
            if seconds < 0.3:
                log("语音太短（%.1fs），跳过" % seconds)
                return ""
            if seconds > VOICE_MAX_SECONDS:
                log("语音太长（%.0fs > %.0fs），跳过" % (seconds, VOICE_MAX_SECONDS))
                return ""
            wav_path = os.path.splitext(path)[0] + ".wav"
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(VOICE_SAMPLE_RATE)
                w.writeframes(raw)
            with open(wav_path, "rb") as f:
                text = asr_transcribe(f.read(), VOICE_API_KEY, VOICE_MODEL, VOICE_BASE_URL, VOICE_TIMEOUT)
            text = clean_transcript(text)
            log("语音识别（%.1fs）：%s" % (seconds, text[:60] or "（空）"))
            return text
        except Exception as e:
            log("语音识别失败：", repr(e))
            return ""


    # 2026-10-27 加（主人定「只指定开白名单群，其他群聊天坚决不能碰到」）：
    #   群白名单在桥接最外层做闸门 —— 不在名单里的群，连消息正文都不读，
    #   不调模型、不记录、不回复。私聊默认也不碰，除非显式开 allowPrivate。
    #   配置：config.json 的 wechat.allowGroups / wechat.allowPrivate
    _allow_groups = [str(x).strip() for x in (cfg.get("wechat") or {}).get("allowGroups") or [] if str(x).strip()]
    _allow_private = bool((cfg.get("wechat") or {}).get("allowPrivate"))
    _gate_stats = {"group_blocked": 0, "private_blocked": 0}

    def _chat_permitted(username, display=""):
        """纯判定：这个会话在白名单里吗？不计数、不打印，可以随便调。"""
        is_group = str(username).endswith("@chatroom")
        if is_group:
            if not _allow_groups:
                return False          # 没配白名单 = 一个群都不碰
            for rule in _allow_groups:
                if rule == "*" or rule == username:
                    return True
                if display and rule == display:
                    return True
            return False
        return _allow_private

    def _chat_allowed(username, display):
        """返回 True 才继续处理。群聊必须在白名单里（按 wxid 或群名匹配）。"""
        if _chat_permitted(username, display):
            return True
        is_group = str(username).endswith("@chatroom")
        if is_group:
            _gate_stats["group_blocked"] += 1
            if _gate_stats["group_blocked"] <= 3:
                log("跳过非白名单群：%s（%s）" % (display or "?", username))
        else:
            _gate_stats["private_blocked"] += 1
        return False

    def on_message_inner(msg, listener):
        try:
            username = msg.get("username") or ""
            if not username:
                return
            # 白名单闸门放在最前面：不在名单里的会话，正文一律不读
            if not _chat_allowed(username, msg.get("chat_name") or msg.get("display") or ""):
                return
            if (msg.get("sender_username") or "") == me:
                return
            received_at = float(msg.get("create_time") or 0)
            if received_at and (received_at < START_TS-SKIP_OLDER_THAN or time.time()-received_at > STALE_AFTER):
                return
            mtype = msg.get("type") or ""
            content = (msg.get("content") or "").strip()
            # 2026-10-27 加：这条消息是不是「她真的看过图了」。
            #   群里 requireAt=true 时，桥接拼的图片描述里没有 @，
            #   服务端会判成「没叫她」而不回 —— 见下面 saw_image 的说明。
            saw_image = False
            # 有的微信版本把表情包报成「图片」，正文里还是 <emoji md5=...>：
            # 一律按表情包装死，绝不能送进视觉模型当图片认。
            if "<emoji" in content:
                log("收到表情包（被标成图片），装死（不识别不回）")
                return
            if mtype == "图片":
                # 2026-10-27 加：图片刚发出来时数据库里往往还没写完（微信写库有延迟），
                #   这时 download_image 找不到文件，图片就被静默丢掉。
                #   实测图文连发：文本 seq=...000、图片 seq=...001，只差 1 毫秒，
                #   但桥接处理文本时图片还没落库 —— 她只看到文字，回「发来看看」。
                #   所以这里先等一会儿，边等边重试，直到能取到图或超时。
                desc = ""
                for _wait in (0, 1.0, 2.0, 3.0):
                    if _wait:
                        time.sleep(_wait)
                    desc = describe_image(msg)
                    if desc:
                        break
                if not desc:
                    log("图片等不到（已重试 3 次），这条跳过")
                    return
                content = "（对方发了一张图片，图里是：%s）" % desc
                saw_image = True
            if mtype == "语音":
                said = transcribe_voice(msg)
                if not said:
                    log("语音没识别出内容，先不说话")
                    return
                content = "（对方发了一条语音，说的是：%s）" % said
            if mtype in ("表情", "动画表情"):
                # 表情包发了装死：不去识别、不回话。以前会把表情包当图片认（截图/转码/
                # 送视觉模型），认出来还硬回一句，容易接岔；现在明确要的是「发了就装死」。
                log("收到表情包，装死（不识别不回）")
                return
            if mtype not in ("文本", "语音", "图片", "表情", "动画表情"):
                if "移出群聊" in content or "移出" in content:
                    dead_chats.add(username)
                    log("被移出群聊，停掉这个会话：", display_name(username))
                    return
                # 链接卡片 / 分享卡片：从 XML 里把 url 和 title 抠出来，当成一句带链接的话
                import re as _re
                mu = _re.search(r"<url>(.*?)</url>", content, _re.S)
                mt = _re.search(r"<title>(.*?)</title>", content, _re.S)
                if mu:
                    title = (mt.group(1).strip() if mt else "")[:60]
                    content = "分享了一个链接：%s %s" % (title, mu.group(1).strip())
                else:
                    return
            if ONLY_CHATS:
                try:
                    if display_name(username) not in ONLY_CHATS:
                        return
                except Exception:
                    return
            # 图片/语音/表情的 content 已经被上面替换成描述，别再重置回原始 XML
            if mtype == "文本":
                content = (msg.get("content") or "").strip()
            if not content or content.startswith("<"):
                return
            sender = msg.get("sender_username") or ""
            if sender == me:
                return  # 自己发的
            create_time = float(msg.get("create_time") or 0)
            if create_time and create_time < START_TS - SKIP_OLDER_THAN:
                return
            # 已经放了太久的消息（比如前面堵着几条），不再回
            if create_time and (time.time() - create_time) > STALE_AFTER:
                log("跳过过期消息（%.0fs 前）：%s" % (time.time() - create_time, content[:20]))
                return

            is_group = username.endswith("@chatroom")
            nickname = ""
            try:
                nickname = db.get_nickname(sender) or ""
            except Exception:
                nickname = ""
            if not nickname:
                nickname = "群友" if is_group else "微信好友"

            # 系统号 / 通知不接话：之前会跟「微信团队」对骂，还把通知当成人来问
            if is_system_notice(username, nickname, content):
                if ONLY_CHATS:
                    return
                log("跳过系统通知：", nickname, "：", content[:24])
                return
            # 报警静默期间，主人在这个群里说一句话就解除静默（「直到我回复」）。
            # 放在白名单/系统通知之后、正常回复之前：主人一开口，
            # 她既能恢复报警能力，这条消息本身也照常回。
            if MASTER_WXID and sender == MASTER_WXID and fault_silenced():
                if fault_master_replied(username):
                    log("[!] 主人回话了，解除报警静默，恢复报错能力")
            # 同一人短时间内重复同一句 → 不再回（防刷屏 / 防通知重投）
            if duplicate_guard(username, sender, content):
                log("跳过重复消息：", nickname, "：", content[:20])
                return


            # 「拍了拍」这类系统提示不是人在说话，别接
            if "拍了拍" in content and "<url>" not in content:
                log("跳过拍一拍提示：", content[:24])
                return
            # 只把真正的 @ 当作 at_me；普通句子里提到「鲸鲸」不能误触发。
            # WeChat 会在 @昵称 后插入 U+2005，先换成普通空格再判定。
            import re as _mention_re
            _at_text = content.replace("\u2005", " ")
            addressed = any(
                a and _mention_re.search(
                    r"@\s*" + _mention_re.escape(str(a)) + r"(?=$|[\s，,：:！!~～…?？。\-—])",
                    _at_text,
                )
                for a in aliases
            )
            _at_remainder = _mention_re.sub(r"^[A-Za-z0-9_\-]{4,}:\s*", "", _at_text).strip()
            for _alias in aliases:
                if _alias:
                    _at_remainder = _mention_re.sub(r"@\s*" + _mention_re.escape(str(_alias)), " ", _at_remainder)
            _bare_mention = not _at_remainder.strip(" \t\r\n，,。.!！?？…~～")
            # 群里先说话、再单独发一个 @ → 把上一条问题补回来
            if username.endswith("@chatroom") and addressed and _bare_mention:
                try:
                    # get_messages 按新到旧返回；直接遍历，拿离这个裸 @ 最近的上一条。
                    for m in db.get_messages(username, limit=12):
                        if m.get("type") != "文本":
                            continue
                        if (m.get("sender_username") or "") != sender:
                            continue
                        prev = (m.get("content") or "").strip()
                        prev = __import__("re").sub(r"^[A-Za-z0-9_\-]{4,}:\s*", "", prev).strip()
                        if prev and not any(a in prev for a in aliases):
                            content = prev
                            log("补上一条问题：", prev[:20])
                            break
                except Exception as e:
                    log("补问题失败：", repr(e))
            # 把群消息里的其他 @ 对象一并交给服务端，帮助模型分清“你”是在说谁。
            _mentioned_names = []
            if is_group:
                for _hit in _mention_re.findall(r"@\s*([^\s@，,：:！!~～…?？。—]+)", _at_text):
                    _name = str(_hit).strip()
                    if _name and _name not in aliases and _name not in _mentioned_names:
                        _mentioned_names.append(_name)
            log("收到", ("群" if is_group else "私聊"), display_name(username), "←", nickname, "：", content[:30])
            # 2026-10-27 加：把「她自己是谁」一起告诉服务端。
            #   以前只说发送者是谁，模型不知道「琦」就是自己，
            #   于是有人在群里 @琦—大肥鱼 时她会回「琦这会儿不一定在」。
            payload = {
                "chat_id": username,
                "chat_type": "group" if is_group else "private",
                "self_id": SELF_WXID or "",
                "self_name": SELF_NICK or "",
                "self_aliases": [a for a in aliases if a],
                "user_id": sender or "unknown",
                "senderId": sender or "unknown",
                "sender_id": sender or "unknown",
                "senderName": nickname,
                "nickname": nickname,
                "text": content,
                "msg_id": "%s:%s:%s" % (username, msg.get("shard") or "", msg.get("local_id")),
                "at_me": addressed,
                "saw_image": saw_image,
                "mentioned_names": _mentioned_names,
                "chat_title": display_name(username) if is_group else "",
            }


            try:
                headers = {"x-whale-bridge": "1"}
                if token:
                    headers["x-bot-token"] = token
                def show_progress():
                    # 只在原白名单群发送一次；不向私聊提示，也不刷屏。
                    now_mono = time.monotonic()
                    if not is_group or now_mono - progress_last_sent.get(username, float("-inf")) < 45:
                        return
                    progress_last_sent[username] = now_mono
                    task_context.id = "progress:" + payload["msg_id"]
                    previous_cooldown = send_retry_until.get(username)
                    if not send_group_checked("我确认一下，马上接着回你", username):
                        # 提示没发成不能让正式回复也被冷却挡住。
                        if previous_cooldown is None:
                            send_retry_until.pop(username, None)
                        else:
                            send_retry_until[username] = previous_cooldown
                reply_settings = read_config().get("wechat") or {}
                wait_seconds = max(12, min(60, float(reply_settings.get("replyWaitSeconds", 35))))
                delay_seconds = max(3, min(10, float(reply_settings.get("progressAfterSeconds", 6))))
                r = wait_for_response(
                    lambda: requests.post(bot_url, json=payload, headers=headers,
                                          timeout=(3, max(5, wait_seconds - 3))),
                    show_progress, delay=delay_seconds, total=wait_seconds,
                )
                data = r.json()
            except (TimeoutError, requests.Timeout):
                log("本轮等待超过上限，已停止等待，不重复投递请求")
                send_text("这次还没拿到结果，我不乱答。你补一下原句或背景，我换个方向帮你确认",
                          display_name(username), username)
                return
            except Exception as e:
                log("请求鲸鲸失败：", repr(e))
                # 服务端连不上 / 超时：属于重要故障，值得叫主人。
                # 但只在群里报警（白名单群），且报警后进静默，不会每条消息都吵。
                notify_master_fault("llm_down", "请求服务端失败：%s" % repr(e)[:100])
                return

            # 服务端明确回了「模型调用失败」：这才是重要故障。
            #   · 不能把「她选择不回」（ignore）算故障 —— 那是正常行为。
            #   · 也不能把 content_blocked 算故障 —— 那是内容审核挡了一句，
            #     换句话就能过，属于日常；半夜为这个 @ 主人三次纯属骚扰。
            if data.get("reason") == "llm_error":
                notify_master_fault(
                    "llm_down",
                    "模型返回错误：%s" % str(data.get("reply") or "")[:100],
                )

            if data.get("action") != "reply" or not data.get("reply"):
                if is_group and addressed:
                    log("鲸鲸没接话：", nickname, "→", content[:20], "|", data.get("reason"))
                return

            # 演示模式（服务端没配 Key，说的是内置台词）：提醒一次，别让人以为「没 Key 也能真聊」
            if data.get("mock") and not MOCK_NOTICED["done"]:
                MOCK_NOTICED["done"] = True
                log("[!] 服务端现在是「演示模式」：没配置 API Key，她回的是内置台词，不是真模型。")
                log("    去网页右上角「设置」填一把 Key（DeepSeek / 智谱 都行）就会切成真模型。")

            target = display_name(username)
            bubbles = data.get("bubbles") or [data["reply"]]
            mention = str(data.get("mention") or "").strip()
            # 生成期间又来了新消息 → 这条旧回复就不发了（避免回上一句）
            # 不再因为“有新消息”丢掉旧回复：宁可晚一点，也要回
            if not rate_guard(username):
                log("这一分钟回太多了，先停一下：", target)
                return
            log("回复", ("群" if is_group else "私聊"), target, "→", nickname, "：", (bubbles[0] or "")[:40])

            first = True
            all_delivered = True
            for bubble_index, line in enumerate(bubbles):
                task_context.id = str(data.get("delivery_id") or payload["msg_id"]) + ":" + str(bubble_index)
                if not line:
                    continue
                # 正文以服务端持久化的气泡为准，恢复任务必须逐字相同。
                all_delivered = send_text(line, target, username, quote_text=(content if first and QUOTE_REPLY else None), task_id=task_context.id) and all_delivered
                first = False
                time.sleep(0.6)
            if data.get("delivery_id"):
                requests.post(bot_url.rsplit("/webhook",1)[0]+"/api/delivery/ack", json={"id":data["delivery_id"],"confirmed":bool(all_delivered)}, headers=headers, timeout=5)
        except Exception as e:
            log("处理消息异常：", type(e).__name__)

    # 2026-10-27 加：醒来后往上读。
    #   以前桥接一重启，停机期间群里的消息全被丢掉（SKIP_OLDER_THAN=10 秒一刀切），
    #   她就像失忆一样，明明有人问过话却一声不吭。
    #   现在启动时先把白名单群里最近的对话读进来当上下文：
    #     · 只读白名单群，其他会话一条都不碰
    #     · 只读进记忆，不主动回（回不回由下一条真实消息决定）
    #     · 最后一条如果是别人在叫她（@她 / 问句），才补一句回应
    def catch_up_on_wake(limit=10):
        try:
            targets = recent_chats(limit=5, within=6 * 3600)
        except Exception as e:
            log("醒来读历史：取会话失败", repr(e))
            return
        for u, name in targets:
            try:
                msgs = db.get_messages(u, limit=limit + 2)
            except Exception as e:
                log("醒来读历史：读 %s 失败 %r" % (name, e))
                continue
            msgs = [m for m in msgs if m.get("type") in ("文本", "语音", "图片")]
            msgs = [m for m in msgs if (m.get("sender_username") or "") != me]
            if not msgs:
                continue
            msgs = list(reversed(msgs))[-limit:]      # 按时间正序，最多 10 条
            log("醒来往上读 %s 的 %d 条消息" % (name, len(msgs)))

            def _history_text(m):
                """把一条历史消息变成人能读的一句话。

                图片/语音的 content 是 XML 或二进制，直接塞给模型就是「一串乱码」。
                这里按类型换成描述；图片识别失败也要说清楚「发过一张图」，
                不能让模型以为自己收到的是垃圾数据。
                """
                t = m.get("type")
                raw = (m.get("content") or "").strip()
                if t == "图片":
                    if "<emoji" in raw:
                        return "（发了一个表情包）"
                    try:
                        d = describe_image(m)
                    except Exception:
                        d = ""
                    return "（发了一张图片，图里是：%s）" % d if d else "（发了一张图片，但图没加载出来）"
                if t == "语音":
                    try:
                        said = transcribe_voice(m)
                    except Exception:
                        said = ""
                    return "（发了一条语音，说的是：%s）" % said if said else "（发了一条语音，没听清）"
                if raw.startswith("<"):
                    return ""
                return raw[:300]
            try:
                base = bot_url.rsplit("/webhook", 1)[0]
                r = requests.post(
                    base + "/api/catchup",
                    json={
                        "chat_id": u,
                        "chat_title": name,
                        "chat_type": "group" if u.endswith("@chatroom") else "private",
                        "self_id": SELF_WXID or "",
                        "self_name": SELF_NICK or "",
                        "self_aliases": [a for a in aliases if a],
                        "messages": [
                            {
                                "sender": m.get("sender_username") or "",
                                "name": (db.get_nickname(m.get("sender_username") or "") or "群友"),
                                "type": m.get("type"),
                                "text": _history_text(m),
                                "ts": int(float(m.get("create_time") or 0) * 1000),
                            }
                            for m in msgs
                        ],
                    },
                    headers={"x-whale-bridge": "1"},
                    timeout=120,
                )
                data = r.json() if r.content else {}
                reply = str(data.get("reply") or "").strip()
                if reply:
                    log("醒来后补一句 →", name, "：", reply[:40])
                    send_text(reply, name, u)
                else:
                    log("醒来读完了，先不插话")
            except Exception as e:
                log("醒来读历史：提交失败 %r" % (e,))

    log("开始监听新消息…（Ctrl+C 退出）")
    from chat_executor import ChatExecutor
    executor = ChatExecutor(workers=3,capacity=48,per_chat=8)

    def on_message(msg, listener):
        try:
            username=msg.get("username") or ""
            if not _chat_permitted(username,msg.get("chat_name") or msg.get("display") or ""):
                return
            if (msg.get("sender_username") or "")==me:
                return
            if not executor.submit(username,on_message_inner,msg,listener):
                log("消息队列已满，跳过新任务（不读取正文）")
        except Exception as error:
            log("提交消息处理失败：",type(error).__name__)

    listener = Listener(db, interval=0.4)
    listener.add_all(on_message)
    listener.start()
    # 醒来往上读：等发送通道预热完再跑，别和启动抢资源。
    # 用线程跑，读历史慢也不影响实时消息。
    threading.Thread(
        target=lambda: (time.sleep(6), catch_up_on_wake(10)),
        daemon=True,
        name="catchup",
    ).start()
    def recent_chats(limit=2, within=1800):
        """最近活跃的会话，**只包含白名单里的**。

        2026-10-27 改：以前这里不过滤，主动分享 / 主动搭话 / 离开通知
        都会挑「最近活跃的那个会话」直接发 —— 完全不看群白名单。
        主人要求「其他群聊天坚决不能碰到」，所以出口统一在这里收口：
        不在白名单的会话一律不返回，三个主动行为就都碰不到它们了。
        """
        out = []
        try:
            for s in db.get_sessions(limit=20):
                u = s.get("username") or ""
                if not u or u == "filehelper":
                    continue
                name = display_name(u)
                if not _chat_permitted(u, name):
                    continue
                try:
                    ms = db.get_messages(u, limit=1)
                except Exception:
                    continue
                if not ms:
                    continue
                t = float(ms[0].get("create_time") or 0)
                if t and (time.time() - t) > within:
                    continue
                out.append((u, name))
                if len(out) >= limit:
                    break
        except Exception:
            pass
        return out

    def notify_away():
        for u, name in recent_chats():
            send_text("我先去休息一下，晚点回你～", name, u, force=True)
            NOTIFIED.append((u, name))
            time.sleep(0.3)
        if NOTIFIED:
            log("已通知最近的聊天：", "、".join(n for _, n in NOTIFIED))

    def share_watch():
        """每隔一段时间自己刷 B 站热门，挑一个没分享过的发到最近活跃的群/私聊。"""
        state_file = os.path.join(ROOT, "data", "share-state.json")
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                last = float(json.load(f).get("lastTs") or 0)
        except Exception:
            last = 0.0
        if not last:
            # 首次启动：5 分钟后再发，别一开机就刷屏
            last = time.time() - SHARE_EVERY * 60 + 60
        while True:
            time.sleep(30)
            if not SHARE_ENABLED:
                continue
            if time.time() - last < SHARE_EVERY * 60:
                continue
            if game_running() or away_active() or foreground_is_fullscreen():
                continue
            target = recent_chats(limit=1, within=7200)
            if not target:
                log("没有最近活跃的会话，这次先不分享")
                last = time.time()
                continue
            u, name = target[0]
            try:
                base = bot_url.rsplit("/webhook", 1)[0]
                r = requests.get(base + "/api/share?chat=" + urllib.parse.quote(u), timeout=40)
                data = r.json()
            except Exception as e:
                log("拉取视频失败：", repr(e))
                last = time.time()
                continue
            video = data.get("video")
            last = time.time()
            try:
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump({"lastTs": last}, f)
            except Exception:
                pass
            if not video:
                log("这次没挑到合适的视频")
                continue
            title = str(video.get("title") or "")[:60]
            link = str(video.get("url") or "")
            topic = str(data.get("topic") or "").strip()
            comment = str(data.get("comment") or "").strip()
            if not comment:
                comment = random.choice([
                    "这个看着还行，评论区比视频有意思",
                    "刚刷到，你们看看",
                    "有点意思，我接着刷了",
                ])
            text = "%s\n%s" % (link, comment)
            log("主动分享视频 →", name, str(video.get("title"))[:30])
            send_text(text, name, u)

    def _is_formal_group(username):
        """这个群是不是「正式场合」（社团/协会对外答疑群）。

        2026-10-27 加（主人：「2个示例身份12还有网安协会是正式场合，不要发这些词」）：
        正式群的标记在 config.json 的 wechat.groupPersonas.<群ID>.formal。
        这里每次现读配置，不缓存 —— 调试台改完不用重启桥接就生效。
        """
        try:
            gp = (read_config().get("wechat") or {}).get("groupPersonas") or {}
            entry = gp.get(str(username or ""))
            return bool(isinstance(entry, dict) and entry.get("formal") is True)
        except Exception:
            return False

    def care_watch():
        """主动搭话：没事的时候她自己来问一句「在干啥 / 累不累」。

        和主动分享同一套节奏，但「该不该发」交给服务端判断（时间窗 / 频率 / 每日上限都在
        src/care.js 里，改配置不用改脚本）。这里只管三件事：到点问一次、挑一个不是正在聊的
        会话、把生成的话发出去。
        """
        state_file = os.path.join(ROOT, "data", "care-state.json")

        def load_state():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
            except Exception:
                return {}

        def save_state(st):
            try:
                os.makedirs(os.path.dirname(state_file), exist_ok=True)
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump(st, f, ensure_ascii=False)
            except Exception:
                pass

        while True:
            time.sleep(60)
            if not CARE_ENABLED:
                continue
            if game_running() or away_active() or foreground_is_fullscreen():
                continue
            st = load_state()
            last_ts = float(st.get("lastTs") or 0)
            today = time.strftime("%Y-%m-%d")
            sent = int(st.get("count") or 0) if st.get("day") == today else 0
            base = bot_url.rsplit("/webhook", 1)[0]
            try:
                r = requests.get(
                    base + "/api/care/due",
                    params={"last": int(last_ts * 1000), "sentToday": sent},
                    timeout=15,
                )
                due = r.json() or {}
            except Exception:
                continue
            if not due.get("due"):
                continue
            # 正在聊的会话先别打扰：连续 CARE_QUIET_MIN 分钟内有过消息就跳过
            active = set()
            if CARE_QUIET_MIN > 0:
                try:
                    active = set(u for u, _n in recent_chats(limit=50, within=int(CARE_QUIET_MIN * 60)))
                except Exception:
                    active = set()
            target = None
            try:
                for u, name in recent_chats(limit=8, within=7 * 86400):
                    if u in active:
                        continue
                    # 2026-10-27 加（主人：「2个示例身份12还有网安协会是正式场合」）：
                    #   正式群（社团/协会的对外答疑群）不做主动搭话。
                    #   她主动冒出来问「在干啥呢，累不累」既不专业也打扰人。
                    if _is_formal_group(u):
                        continue
                    target = (u, name)
                    break
            except Exception:
                target = None
            if not target:
                log("主动搭话：最近都在聊，这次先不打扰")
                continue
            u, name = target
            try:
                r2 = requests.get(base + "/api/care", params={"chat": u}, timeout=40)
                text = str((r2.json() or {}).get("text") or "").strip()
            except Exception as e:
                log("生成搭话失败：", repr(e))
                continue
            if not text:
                continue
            log("主动搭话 →", name, text[:30])
            ok = send_text(text, name, u)
            if ok is not False:
                save_state({"lastTs": time.time(), "day": today, "count": sent + 1})


    BATTERY_SENT = {"done": False}

    def battery_watch():
        while True:
            time.sleep(30)
            try:
                import psutil
                b = psutil.sensors_battery()
            except Exception:
                continue
            if not b or b.percent is None:
                continue
            pct = float(b.percent)
            if pct > 10:
                BATTERY_SENT["done"] = False
                continue
            if pct <= LOW_BATTERY_PCT and not BATTERY_SENT["done"]:
                BATTERY_SENT["done"] = True
                log("电量低（%.0f%%），给最近 3 个会话发晚安" % pct)
                try:
                    for u, name in recent_chats(limit=3, within=86400):
                        send_text(GOODNIGHT_TEXT, name, u, force=True)
                        time.sleep(0.4)
                except Exception as e:
                    log("发晚安失败：", repr(e))

    def notify_back():
        for u, name in list(NOTIFIED):
            send_text("我回来啦，刚在打游戏", name, u, force=True)
            time.sleep(0.3)
        NOTIFIED.clear()

    def game_watch():
        while True:
            time.sleep(4)
            running = game_running()
            if running and not AWAY["on"]:
                AWAY["on"] = True
                log("检测到游戏运行，进入休息模式（回复先排队）")
                if AWAY_NOTICE and not game_running():
                    try:
                        notify_away()
                    except Exception as e:
                        log("休息通知失败：", repr(e))
            elif not running and AWAY["on"]:
                AWAY["on"] = False
                log("游戏已退出，补发排队消息")
                with pending_lock:
                    items = list(pending_replies.values())
                    pending_replies.clear()
                now = time.time()
                fresh = [x for x in items if now - x[3] <= QUEUED_MAX_AGE]
                dropped = len(items) - len(fresh)
                fresh = fresh[-QUEUED_MAX_CHATS:]
                if dropped:
                    log("排队里有 %d 条已经太旧（>%ds），不补发" % (dropped, int(QUEUED_MAX_AGE)))
                for text, target, uname, _ts in fresh:
                    send_text(text, target, uname, force=True)
                if RETURN_NOTICE:
                    try:
                        notify_back()
                    except Exception as e:
                        log("回来通知失败：", repr(e))

    def digest_watch():
        last = time.time()
        while True:
            time.sleep(30)
            if DIGEST_MINUTES <= 0 or not MASTER_WXID:
                continue
            if time.time() - last < DIGEST_MINUTES * 60:
                continue
            if AWAY["on"] or away_active() or game_running() or foreground_is_fullscreen():
                continue
            try:
                base = bot_url.rsplit("/webhook", 1)[0]
                r = requests.get(base + "/api/digest", params={"since": int(last * 1000)}, timeout=30)
                data = r.json()
            except Exception as e:
                log("拉取消息汇总失败：", repr(e))
                continue
            if data.get("count") and data.get("text"):
                name = display_name(MASTER_WXID) or "主人"
                log("给主人发消息汇总：%d 条" % data["count"])
                if send_text(data["text"], name, MASTER_WXID, force=True):
                    last = time.time()
            else:
                last = time.time()

    def explain_uia_failure():
        """UIA 拿不到时把原因说清楚。

        「热激活 UIA 失败」有三种完全不同的原因，光看那一句没法修：
          A. 主窗所在进程里枚举不到 Weixin.dll（挑到子进程 / 权限 / 微信刚重启）
          B. 这个微信版本库还不支持（特征扫描没命中、版本表里也没有）
          C. 补丁写进去了但 mmui 控件没出现（校验失败，已回滚）
        """
        try:
            import win32process
            from wechatauto.accessibility import AccessibilityGate, QACCESSIBLE_ACTIVE_RVA_BY_VERSION
        except Exception as e:
            log("UIA 诊断不可用：", repr(e))
            return
        hwnd = find_weixin_hwnd()
        if not hwnd:
            log("UIA 诊断：找不到标题为「微信」的主窗（微信缩在托盘？先点开它）")
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            mods = list(AccessibilityGate._process_modules(pid) or [])
        except Exception as e:
            log("UIA 诊断：枚举模块失败：", repr(e))
            return
        dll = None
        for _b, _s, name, path in mods:
            if (name or "").lower() == "weixin.dll":
                dll = path
                break
        if not dll:
            log("UIA 诊断（原因A）：主窗进程 pid=%s 里只有 %d 个模块，枚举不到 Weixin.dll" % (pid, len(mods)))
            log("    → 多见于：微信刚重启、主窗属于子进程、或与微信权限不一致")
            return
        ver = os.path.basename(os.path.dirname(dll))
        try:
            cands = AccessibilityGate._qaccessible_candidate_rvas(dll)
        except Exception as e:
            log("UIA 诊断：取候选 RVA 失败：", repr(e))
            return
        known = QACCESSIBLE_ACTIVE_RVA_BY_VERSION.get(ver)
        if not cands:
            log("UIA 诊断（原因B）：微信 %s 没有任何可用 gate RVA，这个版本库还不支持" % ver)
            log("    → 升级 wechatauto-replica，或换回 4.1.13.65 / 4.1.15.53")
        else:
            log("UIA 诊断（原因C）：微信 %s，候选 RVA %d 个（%s），补丁写了但 mmui 没出现" % (
                ver, len(cands),
                "版本表命中" if known is not None else "靠特征扫描",
            ))
            log("    → 重启微信后立刻重试；仍失败说明该版本的保护变了，等库更新")

    def warmup():
        """界面引擎延迟到实际发送时初始化，后台启动不激活微信。"""
        log("发送通道采用按需初始化：后台预热不再拉起微信窗口")

    def send_watchdog():
        """发送卡住超过 40 秒就重启桥接，避免整个队列卡死。"""
        while True:
            time.sleep(5)
            if SEND_STATE["active"] and (time.monotonic() - SEND_STATE["start"]) > 40:
                log("发送卡住超过 40 秒，强制重启桥接进程…")
                # 不依赖发送线程的 finally；退出前独立、非阻塞地恢复窗口。
                try:
                    snapshot=SEND_STATE.get("snapshot")
                    cleanup=threading.Thread(target=lambda:restore_win_state(snapshot),daemon=True)
                    cleanup.start()
                    cleanup.join(1)
                finally:
                    SEND_STATE["active"] = False
                # 用 7 而不是 2：2 是「找不到数据库目录」这种优雅报错（不该被守护循环
                # 无限重启），7 才是「我自己要重启」—— 启动脚本看到 7 就连着重启。
                os._exit(7)

    def recover_deliveries():
        """从服务端恢复待投递任务，过期任务保留人工处理，未知提交仅回读。"""
        base=bot_url.rsplit("/webhook",1)[0]
        headers={"x-whale-bridge":"1", "x-bot-token":token}
        while True:
            time.sleep(30)
            if AWAY["on"] or away_active() or game_running() or foreground_is_fullscreen():
                continue
            try:
                result=requests.get(base+"/api/delivery/pending",headers=headers,timeout=5).json()
                for item in result.get("items",[]):
                    group_id=str(item.get("chatId") or "")
                    if not _chat_permitted(group_id,display_name(group_id)) or not group_id.endswith("@chatroom"):
                        continue
                    if time.time()*1000-float(item.get("createdAt",0))>QUEUED_MAX_AGE*1000:
                        continue
                    # 生成完成不等于已发；逐气泡使用稳定任务键，日志会挡住未知提交重发。
                    complete=True
                    for index,line in enumerate(item.get("bubbles") or []):
                        if line:
                            complete=send_text(str(line),display_name(group_id),group_id,
                                task_id=str(item["id"])+":"+str(index)) and complete
                    if item.get("bubbles"):
                        requests.post(base+"/api/delivery/ack",headers=headers,
                            json={"id":item["id"],"confirmed":complete},timeout=5)
            except Exception as error:
                log("待投递任务恢复暂缓：",type(error).__name__)

    def flush_pending():
        while True:
            time.sleep(2)
            if AWAY["on"] or away_active() or foreground_is_fullscreen():
                continue
            with pending_lock:
                if not pending_replies:
                    continue
                items = list(pending_replies.items())
            now = time.time()
            for key, item in items[:QUEUED_MAX_CHATS]:
                text, target, uname, timestamp, task_id = item
                if now-timestamp > QUEUED_MAX_AGE:
                    with pending_lock:
                        pending_replies.pop(key, None)
                    continue
                task_context.id = task_id
                if send_text(text, target, uname, force=True, task_id=task_id):
                    with pending_lock:
                        pending_replies.pop(key, None)

    def listener_alive():
        """收消息的轮询线程是否还活着。"""
        try:
            th = getattr(listener, "_thread", None)
            return bool(th is not None and th.is_alive())
        except Exception:
            return False

    def listener_watch():
        """轮询线程死了就把它拉起来。

        症状：桥接进程还在（心跳正常）、微信里发消息也进来了，但一条都不回复 ——
        因为读数据库的那个线程悄悄退出了。以前只能重启桥接。
        """
        misses = 0
        while True:
            time.sleep(15)
            if listener_alive():
                misses = 0
                continue
            misses += 1
            if misses < 2:
                continue
            log("[!] 收消息线程已经不在了，自动重启监听…")
            try:
                listener.start()
                if listener_alive():
                    log("监听已重启，继续收消息")
                else:
                    log("监听重启后仍未启动，建议重启桥接")
            except Exception as e:                 # noqa: BLE001
                log("监听重启失败：", repr(e))
            misses = 0

    probe_guard=threading.Lock()
    ui_probe_state={}
    def inspect_send_channel():
        """仅由本机认证请求触发，不切群、不粘贴、不发送。"""
        if not probe_guard.acquire(False):
            return
        def inspect():
            nonlocal gui
            with ui_operation():
                hwnd=find_weixin_hwnd()
                if not hwnd or not account_owns_hwnd(hwnd):
                    UIA_CACHE.update(u=None,checked=time.monotonic())
                    return {"ready":False,"inputReadable":False,"hasDraft":False,"checkedAt":time.time()*1000}
                import ctypes
                ctypes.windll.user32.ShowWindowAsync(hwnd,9)
                time.sleep(.2)
                with gui_lock:
                    if gui is None:gui=create_wechat_gui()
                    engine=gui._get_uia(refresh=True)
                if engine is not None:
                    install_quiet_uia(engine)
                UIA_CACHE.update(u=engine,checked=time.monotonic())
                entry=engine._chat_input() if engine is not None else None
                value=engine._read_edit_value(entry) if entry is not None else None
                return {"ready":engine is not None,"inputReadable":value is not None,"hasDraft":bool(value),"checkedAt":time.time()*1000}
        try:
            result=ui_executor.call(inspect)
            ui_probe_state.clear();ui_probe_state.update(result)
        except Exception as error:
            log("只读界面自检失败：",type(error).__name__)
        finally:
            probe_guard.release()

    def bridge_heartbeat():
        """每 20 秒告诉服务端「桥接还活着、UIA/ffmpeg 状态如何」。

        没有心跳时，服务端只知道「它收到了消息」，不知道回复到底有没有能力发出去；
        网页上也就没法提示「桥接离线」或「UIA 不可用」。
        """
        import shutil as _shutil

        nonlocal gui
        base = bot_url.rsplit("/webhook", 1)[0]
        misses = 0
        gate_logged = False
        while True:
            try:
                # 游戏在跑时桥接会跳过预热、进休息模式，这时 UIA 还没初始化，
                # 不能当成「UIA 坏了」上报 —— 不然网页会误报「消息发不出去」。
                resting = bool(game_running())
                # 心跳只报告状态，禁止预热、最大化和前台切换。
                # _get_uia() 内部会激活微信；以前持续重试让用户无法收起窗口。
                uia_now = None
                if not resting:
                    if not wechat_window_alive():
                        # 不持有 gui_lock 等待正在卡住的 UIA 线程，心跳独立上报。
                        UIA_CACHE["u"] = None
                        gui = None
                        uia_now = False
                    elif UIA_CACHE.get("checked"):
                        uia_now = UIA_CACHE.get("u") is not None
                payload = {
                    "platform": "wechat",
                    "uia": uia_now,
                    "rest": resting,
                    # 收消息线程还活着吗：死了就是「能心跳、但一条消息都不进来」
                    "listener": listener_alive(),
                    "ffmpeg": bool(_shutil.which("ffmpeg")),
                    "probe": dict(ui_probe_state),
                    "uiQueue": ui_executor.queue.qsize(),
                    "chatQueue": executor.count,
                    "uiTaskAgeSec": round(time.monotonic()-SEND_STATE["start"],1) if SEND_STATE["active"] else 0,
                    "uiaCheckedSec": round(time.monotonic()-UIA_CACHE["checked"],1) if UIA_CACHE.get("checked") else None,
                }
                headers = {"x-whale-bridge": "1"}
                if token:
                    headers["x-bot-token"] = token
                reply=requests.post(base + "/api/bridge/heartbeat", json=payload, headers=headers, timeout=8).json()
                if reply.get("probeUi"):
                    threading.Thread(target=inspect_send_channel,name="whale-readonly-probe",daemon=True).start()
            except Exception:
                pass  # 服务没起来/重启中，下一轮再报
            time.sleep(20)

    def config_watch():
        """盯 config.json 的修改时间：调试台一保存就热更新，不用重启桥接。

        以前这些开关只在启动时读一次，用户在 /debug 里关掉「主动分享」、
        改「忽略名单」、「游戏白名单」都没反应，只能重启桥接 —— 也就是
        「调了没用，还是默认」。
        """
        last = 0.0
        try:
            last = os.path.getmtime(CONFIG_PATH)
        except Exception:
            last = 0.0
        while True:
            time.sleep(5)
            try:
                mt = os.path.getmtime(CONFIG_PATH)
            except Exception:
                continue
            if mt <= last:
                continue
            last = mt
            if reload_config():
                log("配置已热更新（调试台改完即时生效，不用重启桥接）")

    threading.Thread(target=flush_pending, daemon=True).start()
    threading.Thread(target=config_watch, daemon=True).start()
    threading.Thread(target=game_watch, daemon=True).start()
    threading.Thread(target=digest_watch, daemon=True).start()
    threading.Thread(target=warmup, daemon=True).start()
    threading.Thread(target=send_watchdog, daemon=True).start()
    threading.Thread(target=recover_deliveries, daemon=True).start()
    threading.Thread(target=battery_watch, daemon=True).start()
    threading.Thread(target=share_watch, daemon=True).start()
    threading.Thread(target=care_watch, daemon=True).start()
    threading.Thread(target=bridge_heartbeat, daemon=True).start()
    threading.Thread(target=listener_watch, daemon=True).start()
    log("已启动。鲸鲸地址：", bot_url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("正在退出…")
        listener.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
