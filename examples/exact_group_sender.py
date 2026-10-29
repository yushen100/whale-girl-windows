"""精确群发送：禁止成员名/模糊别名，提交一次后只确认，不重复发送。"""
from dataclasses import dataclass
import time


@dataclass
class Delivery:
    confirmed: bool = False
    attempted: bool = False
    reason: str = ""


def send_exact_group(uia, db, group_id, text, self_id, sleep=time.sleep, journal=None, task_id=""):
    if not str(group_id).endswith("@chatroom"):
        return Delivery(reason="拒绝非群目标")
    key = journal.key(group_id, text, task_id) if journal else None
    prior = journal.get(key) if journal else {"state": "new"}
    if prior.get("state") == "confirmed":
        return Delivery(confirmed=True, attempted=True, reason="投递日志已确认，不重复发送")
    if prior.get("state") == "submitted":
        # 崩溃或超时后的未知提交只查原群，不能重复 Enter。
        rows = db.get_messages(group_id, limit=100) or []
        matched = any(int(row.get("sort_seq") or 0) > int(prior.get("watermark", 0))
                      and str(row.get("sender_username") or "") == str(self_id)
                      and str(row.get("content") or "").strip() == str(text).strip() for row in rows)
        if matched:
            journal.set(key, state="confirmed")
        return Delivery(confirmed=matched, attempted=True, reason="未知提交仅核验，禁止重发")
    name = str(db.group_id_to_name(group_id) or "").strip()
    if not name:
        return Delivery(reason="群没有明确名称，无法安全定位")
    contacts = db.search_contact(name) or []
    identities = {str(row.get("username") or "") for row in contacts
                  if name in (str(row.get("nick_name") or "").strip(),
                              str(row.get("remark") or "").strip())}
    if identities != {group_id}:
        return Delivery(reason="群名不能唯一映射到原群 ID，拒绝发送")
    if uia.current_chat() != name:
        if not uia.open_chat(name, section="群聊", retries=1):
            return Delivery(reason="没有打开精确目标群")
    if uia.current_chat() != name:
        return Delivery(reason="当前会话不是目标群，禁止输入")
    entry = uia._chat_input()
    if entry is None:
        return Delivery(reason="找不到目标群输入框")
    reader = getattr(uia, "_read_edit_value", None)
    draft = reader(entry) if reader else None
    if draft is None:
        return Delivery(reason="输入框不可回读，禁止盲目粘贴或发送")
    if str(draft).strip():
        return Delivery(reason="输入框已有草稿，保留人工内容，暂缓发送")
    before = db.get_messages(group_id, limit=8) or []
    watermark = max((int(row.get("sort_seq") or 0) for row in before), default=0)
    uia._paste_into(entry, text, clear=True)
    if uia.current_chat() != name:
        return Delivery(reason="输入期间会话发生变化，禁止发送")
    landed, _, _ = uia._paste_landed(entry, text)
    actual = reader(entry)
    if actual is None or str(actual).replace("\r\n","\n").strip() != str(text).replace("\r\n","\n").strip():
        return Delivery(reason="输入框全文核验失败，禁止发送；保留草稿供检查")
    if not landed:
        return Delivery(reason="目标群输入框内容校验失败")
    if uia.current_chat() != name:
        return Delivery(reason="提交前会话发生变化，禁止发送")
    if journal:
        journal.set(key, state="submitted", watermark=watermark)
    result = Delivery(attempted=True, reason="已提交，尚未在原群数据库确认")
    try:
        uia._send_attempted = True
        entry.SendKeys("{Enter}", waitTime=0.05)
    except Exception:
        # Enter 抛错也可能已投递；只做数据库确认，不重按。
        pass
    for _ in range(12):
        sleep(0.5)
        try:
            rows = db.get_messages(group_id, limit=8) or []
            if any(int(row.get("sort_seq") or 0) > watermark
                   and str(row.get("sender_username") or "") == str(self_id)
                   and str(row.get("content") or "").strip() == str(text).strip()
                   for row in rows):
                if journal:
                    journal.set(key, state="confirmed")
                return Delivery(confirmed=True, attempted=True, reason="原群 ID 已回读确认")
        except Exception:
            pass
    return result
