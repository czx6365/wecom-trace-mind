"""企业微信推送层：群机器人 webhook + 风险告警。"""

import json
import os
import urllib.request

from .db import now_iso

RISK_LEVEL_NAMES = {"1": "一级·提醒", "2": "二级·警告", "3": "三级·高风险"}


def push_wechat_markdown(content, webhook_url=None):
    """推送 markdown 到企业微信群机器人，返回 True；未配置返回 False。"""
    url = (webhook_url or os.environ.get("WECHAT_WEBHOOK_URL", "")).strip()
    if not url:
        return False

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": content[:4000],
        },
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errcode") != 0:
        raise RuntimeError(f"企业微信推送失败：{json.dumps(result, ensure_ascii=False)}")
    return True


def resolve_risk_webhook():
    """风险告警 webhook：优先 RISK_WEBHOOK_URL，缺省回退 WECHAT_WEBHOOK_URL。"""
    url = os.environ.get("RISK_WEBHOOK_URL", "").strip()
    if url:
        return url
    return os.environ.get("WECHAT_WEBHOOK_URL", "").strip() or ""


def build_risk_markdown(event, message):
    """把一条风险事件 + 原消息拼成告警 markdown。"""
    level_name = RISK_LEVEL_NAMES.get(str(event.get("risk_level")), "风险")
    lines = [
        f"## ⚠️ 社群风险告警：{level_name}",
        "",
        f"> 类别：{event.get('key_field') or '其他违规'}",
    ]
    if str(event.get("risk_level")) == "3":
        lines.append("> **请立即人工确认并处理**")
    lines.extend(
        [
            "",
            f"**群：** {message.get('source_group')}",
            f"**发送者：** {message.get('sender_name') or message.get('sender_key') or '未知'}",
            f"**时间：** {event.get('created_at')}",
            "",
            f"**原文：** {message.get('content', '')[:200]}",
        ]
    )
    if event.get("value_text"):
        lines.append(f"**详情：** {event['value_text'][:200]}")
    return "\n".join(lines)


def push_risk_alert(event, message):
    """推送一条风险告警。

    返回 (status, error)：status ∈ {'pushed', 'failed', 'not_needed'}。
    """
    webhook_url = resolve_risk_webhook()
    if not webhook_url:
        return "not_needed", "未配置风险告警 webhook（RISK_WEBHOOK_URL / WECHAT_WEBHOOK_URL）"

    try:
        pushed = push_wechat_markdown(build_risk_markdown(event, message), webhook_url)
        if pushed:
            return "pushed", None
        return "not_needed", "webhook 未配置"
    except Exception as exc:
        return "failed", str(exc)


def push_risk_event(event_id, webhook_url=None):
    """重推一条已入库的风险事件，返回 (ok, error)。"""
    from .db import get_db, rows_to_dicts

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT e.*, m.content, m.sender_name, m.sender_key AS message_sender_key
            FROM events e JOIN messages m ON m.id = e.message_id
            WHERE e.id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            return False, "事件不存在"
        event = dict(row)
        message = {
            "source_group": event["source_group"],
            "sender_name": event["sender_name"],
            "sender_key": event["message_sender_key"],
            "content": event["content"],
        }

        url = (webhook_url or resolve_risk_webhook()).strip()
        if not url:
            return False, "未配置风险告警 webhook"
        try:
            pushed = push_wechat_markdown(build_risk_markdown(event, message), url)
        except Exception as exc:
            return False, str(exc)
        if not pushed:
            return False, "webhook 未配置"

        conn.execute(
            "UPDATE events SET push_status = 'pushed', pushed_at = ? WHERE id = ?",
            (now_iso(), event_id),
        )
        # 同步 messages.risk_pushed，防止重建分类后重复推送
        _append_risk_pushed(conn, event["message_id"], event["risk_level"])
        return True, None


def _append_risk_pushed(conn, message_id, level):
    if not level:
        return
    row = conn.execute(
        "SELECT risk_pushed FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return
    levels = {part for part in (row["risk_pushed"] or "").split(",") if part}
    levels.add(str(level))
    conn.execute(
        "UPDATE messages SET risk_pushed = ? WHERE id = ?",
        (",".join(sorted(levels)), message_id),
    )
