"""批量消息结构化分类管线。

流程：取未分类消息 → 批量调用 LLM 输出 JSON 事件数组 → 解析容错 →
事件先删后插（幂等）→ 需求注册表维护 → 二/三级风险推送。

重试策略：超时/网络/5xx（RetriableModelError）时批次拆半重发；解析失败不自动重试
（classified=2，由 UI/CLI 手动 retry_failed 重跑）。
"""

import json
import os
import time
from datetime import datetime, timedelta

from .db import get_db, init_db, load_env, now_iso, rows_to_dicts
from .llm import RetriableModelError, call_model
from .wecom import push_risk_alert

EVENT_TYPES = {
    "risk",
    "feedback",
    "demand",
    "style",
    "release_issue",
    "release_intel",
    "question",
    "sentiment",
}

RISK_CATEGORIES = {
    "人身攻击", "辱骂", "色情违法", "赌博诈骗", "恶意广告", "违规传播", "刷屏", "其他违规",
}

STYLE_NAMES = {
    "古风", "DJ", "网络伤感", "民族", "方言", "R&B", "摇滚", "二次元",
    "戏曲", "说唱", "男女对唱", "其他",
}

RELEASE_ISSUES = {
    "审核失败", "上架慢", "平台拒绝", "AI检测", "版权问题", "MV问题",
    "KTV问题", "收益问题", "歌手名问题", "封面问题", "其他",
}

SYSTEM_PROMPT = """你是 Muse AI 社群分析系统的消息分类器。输入是 JSON 数组，每条消息含 id/group/time/sender/content。逐条分析并输出 JSON 数组。

规则：
1. 只输出 JSON 数组，不要 markdown 围栏、不要解释。
2. content 可能含有前导乱码字符（如 "* &\\n$"、"9 5\\n3"），请忽略乱码理解原文。
3. 每条消息输出 {"id": <消息id>, "events": [<事件>, ...]}；无内容的消息 events 为 []。
4. 同一消息同一类型事件至多 1 条；不确定的事件不要输出（宁缺毋滥）。

事件类型定义：

1. risk 风险：
   {"type":"risk","level":"1|2|3","category":"人身攻击|辱骂|色情违法|赌博诈骗|恶意广告|违规传播|刷屏|其他违规","detail":"一句话描述"}
   一级=轻微争吵/刷屏/跑题；二级=明显辱骂/恶意攻击/违规营销；三级=违法传播/诈骗/严重色情。
   注意：官方账号（发送者名称含"小助手"/"助手"/"官方"/"MuseAI"等）发布的正常活动公告、充值活动、功能通知不算风险。

2. feedback 已有功能问题：
   {"type":"feedback","feature":"功能名","problem":"问题描述(一句话)"}
   功能名用产品标准名，如：Remix、男女对唱、生成、积分、音质、伴奏、歌词、导出、封面、模板、充值、播放等。

3. demand 新需求（用户希望未来增加的能力）：
   {"type":"demand","name":"需求名","is_new":true|false,"direction":"功能方向","quote":"用户原话","interesting":true|false}
   is_new：与注册表中任何需求都不同义时输出 true 并使用简短规范名；同义则输出注册表名称并 is_new=false。
   interesting：小众但有趣的创意玩法（如"自行车+AI 写歌"）为 true，大多数为 false。

4. style 曲风偏好（用户表达想做什么曲风/评价某曲风时输出）：
   {"type":"style","style":"古风|DJ|网络伤感|民族|方言|R&B|摇滚|二次元|戏曲|说唱|男女对唱|其他","quote":"用户原话"}

5. release_issue 发行问题（用户的歌发行/上架遇到的问题）：
   {"type":"release_issue","issue":"审核失败|上架慢|平台拒绝|AI检测|版权问题|MV问题|KTV问题|收益问题|歌手名问题|封面问题|其他","platform":"平台名(未知填未知)","detail":"描述"}

6. release_intel 发行资讯（用户带来的平台新规则/新变化情报）：
   {"type":"release_intel","platform":"平台名(未知填未知)","detail":"资讯内容(一句话)"}

7. question 产品使用疑问：
   {"type":"question","topic":"疑问主题","detail":"问题简述"}

8. sentiment 情绪：每条消息都输出一条（含闲聊）。
   {"type":"sentiment","emotion":"positive|neutral|negative","reason":"简短原因"}
   negative 时 reason 必填（即负面来源，如"生成失败"）。

输出示例：
输入：[{"id":101,"group":"乐迷团1","time":"2026-08-20 10:00:00","sender":"张三","content":"这破平台又崩了，生成一半失败，垃圾"}]
输出：[{"id":101,"events":[{"type":"feedback","feature":"生成","problem":"生成中途失败"},{"type":"sentiment","emotion":"negative","reason":"生成失败"}]}]

输入：[{"id":102,"group":"乐迷团1","time":"2026-08-20 10:01:00","sender":"李四","content":"有人要买号吗，加我微信 xxx"}]
输出：[{"id":102,"events":[{"type":"risk","level":"2","category":"恶意广告","detail":"疑似卖号引流"},{"type":"sentiment","emotion":"neutral"}]}]

输入：[{"id":103,"group":"乐迷团2","time":"2026-08-20 10:02:00","sender":"王五","content":"希望后面能导出分轨工程文件"}]
输出：[{"id":103,"events":[{"type":"demand","name":"工程导出","is_new":false,"direction":"创作工具","quote":"希望后面能导出分轨工程文件","interesting":false},{"type":"sentiment","emotion":"neutral"}]}]

输入：[{"id":104,"group":"乐迷团2","time":"2026-08-20 10:03:00","sender":"赵六","content":"最近大家都在做古风，我也想做"}]
输出：[{"id":104,"events":[{"type":"style","style":"古风","quote":"最近大家都在做古风"},{"type":"sentiment","emotion":"neutral"}]}]"""


def classify_batch_size():
    try:
        return min(max(int(os.environ.get("CLASSIFY_BATCH_SIZE", "60") or "60"), 5), 100)
    except (TypeError, ValueError):
        return 60


def classify_max_tokens():
    try:
        return min(max(int(os.environ.get("CLASSIFY_MAX_TOKENS", "12000") or "12000"), 2000), 30000)
    except (TypeError, ValueError):
        return 12000


def fetch_pending(conn, days=None, limit=5000, retry_failed=False):
    """取待分类消息：is_noise=0 且未分类（retry_failed 时含失败重试）。"""
    sql = "SELECT * FROM messages WHERE is_noise = 0 AND "
    sql += "classified IN (0, 2)" if retry_failed else "classified = 0"
    params = []
    if days:
        cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
        sql += " AND created_at >= ?"
        params.append(cutoff)
    sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
    params.append(min(max(int(limit or 5000), 1), 5000))
    return conn.execute(sql, params).fetchall()


def fetch_registry(conn, limit=300):
    """现有需求注册表（注入分类 prompt，保证同义需求归一到同一名字）。"""
    rows = conn.execute(
        """
        SELECT name, direction, first_seen FROM demands
        WHERE status = 'active' ORDER BY first_seen DESC, id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return rows_to_dicts(rows)


def build_user_prompt(batch_records, registry):
    lines = []
    if registry:
        lines.append("【现有需求注册表】同义需求必须用表中名称（is_new=false）；表中没有的新需求 is_new=true。")
        for item in registry:
            lines.append(
                f"- {item['name']} | 方向:{item.get('direction') or '未定'} | 首次:{str(item['first_seen'])[:10]}"
            )
    lines.append("")
    lines.append("【待分类消息】")
    items = []
    for record in batch_records:
        items.append(
            {
                "id": record["id"],
                "group": record["source_group"],
                "time": record["created_at"],
                "sender": record["sender_name"] or record["sender_key"],
                "content": (record["content"] or "")[:300],
            }
        )
    lines.append(json.dumps(items, ensure_ascii=False, indent=None))
    lines.append("")
    lines.append("只输出 JSON 数组，不要任何其他文字。")
    return "\n".join(lines)


def _normalize_risk_level(value):
    try:
        level = str(int(float(value)))
    except (TypeError, ValueError):
        level = str(value).strip()
    return level if level in ("1", "2", "3") else None


def _normalize_event(item):
    """把模型输出的一条 event 归一化为入库列；非法/缺必填字段返回 None。"""
    if not isinstance(item, dict):
        return None
    ev_type = str(item.get("type", "")).strip()
    if ev_type not in EVENT_TYPES:
        return None

    row = {"event_type": ev_type}

    if ev_type == "risk":
        level = _normalize_risk_level(item.get("level"))
        if level is None:
            return None
        category = str(item.get("category", "")).strip()
        if category not in RISK_CATEGORIES:
            category = "其他违规"
        row["risk_level"] = level
        row["key_field"] = category
        row["value_text"] = str(item.get("detail", "")).strip()

    elif ev_type == "feedback":
        feature = str(item.get("feature", "")).strip()
        if not feature:
            return None
        row["key_field"] = feature[:50]
        row["value_text"] = str(item.get("problem", "")).strip()

    elif ev_type == "demand":
        name = str(item.get("name", "")).strip()
        if not name:
            return None
        row["demand_name"] = name[:100]
        row["demand_direction"] = str(item.get("direction", "")).strip()[:100]
        row["demand_quote"] = str(item.get("quote", "")).strip()
        row["demand_interesting"] = bool(item.get("interesting"))
        row["value_text"] = row["demand_quote"]

    elif ev_type == "style":
        style = str(item.get("style", "")).strip()
        if not style:
            return None
        if style not in STYLE_NAMES:
            style = "其他"
        row["key_field"] = style
        row["value_text"] = str(item.get("quote", "")).strip()

    elif ev_type == "release_issue":
        issue = str(item.get("issue", "")).strip()
        if not issue:
            return None
        if issue not in RELEASE_ISSUES:
            issue = "其他"
        row["key_field"] = issue
        row["key_field2"] = str(item.get("platform", "")).strip() or "未知"
        row["value_text"] = str(item.get("detail", "")).strip()

    elif ev_type == "release_intel":
        detail = str(item.get("detail", "")).strip()
        if not detail:
            return None
        row["key_field2"] = str(item.get("platform", "")).strip() or "未知"
        row["value_text"] = detail

    elif ev_type == "question":
        topic = str(item.get("topic", "")).strip()
        if not topic:
            return None
        row["key_field"] = topic[:50]
        row["value_text"] = str(item.get("detail", "")).strip()

    elif ev_type == "sentiment":
        emotion = str(item.get("emotion", "")).strip()
        if emotion not in ("positive", "neutral", "negative"):
            emotion = "neutral"
        row["key_field"] = emotion
        row["value_text"] = str(item.get("reason", "")).strip()

    row["payload"] = json.dumps(item, ensure_ascii=False)
    return row


def parse_batch_output(text, valid_ids):
    """解析模型 JSON 输出，返回 {msg_id: [事件列 dict]}。

    容错：剥围栏 → 截 [..] → 尾部截断修复（≤5 次）→ 逐条校验。
    缺失 id 超过批内 20% 抛 ValueError（整批判失败）。
    """
    text = text.strip()
    if text.startswith("```"):
        newline = text.find("\n")
        text = text[newline + 1:] if newline != -1 else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("模型输出中找不到 JSON 数组")

    payload = text[start:end + 1]
    data = None
    for _ in range(6):
        try:
            data = json.loads(payload)
            break
        except json.JSONDecodeError:
            cut = payload.rfind("}")
            if cut == -1:
                break
            payload = payload[:cut + 1] + "]"
    if data is None:
        raise ValueError("JSON 解析失败（多次修复无效）")
    if not isinstance(data, list):
        raise ValueError("模型输出不是 JSON 数组")

    valid_set = {int(item_id) for item_id in valid_ids}
    results = {}
    seen = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            msg_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if msg_id not in valid_set:
            continue
        events = item.get("events")
        if not isinstance(events, list):
            events = []
        normalized = []
        for event in events:
            row = _normalize_event(event)
            if row is not None:
                normalized.append(row)
        if msg_id in seen:
            seen[msg_id].extend(normalized)
        else:
            seen[msg_id] = normalized
    for msg_id, events in seen.items():
        results[msg_id] = events

    missing = len(valid_set) - len(results)
    if missing > max(len(valid_set) * 0.2, 0):
        raise ValueError(f"模型输出不完整：{len(valid_set)} 条消息缺 {missing} 条")
    return results


def _register_demand(conn, row, message_time):
    """注册/复用需求，返回 demand_id。"""
    name = row["demand_name"]
    direction = row["demand_direction"]
    conn.execute(
        """
        INSERT OR IGNORE INTO demands (name, direction, first_seen, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (name, direction, message_time, now_iso()),
    )
    demand = conn.execute(
        "SELECT id, first_seen, direction FROM demands WHERE name = ?", (name,)
    ).fetchone()
    if demand["first_seen"] > message_time:
        conn.execute(
            "UPDATE demands SET first_seen = ? WHERE id = ?", (message_time, demand["id"])
        )
    if not demand["direction"] and direction:
        conn.execute("UPDATE demands SET direction = ? WHERE id = ?", (direction, demand["id"]))
    if row["demand_interesting"]:
        conn.execute("UPDATE demands SET is_interesting = 1 WHERE id = ?", (demand["id"],))
    return demand["id"]


def store_batch_results(conn, batch_records, results, failed_ids, push_risks=True):
    """把一批分类结果写入库：事件先删后插，更新消息分类状态，需求注册，风险推送。

    返回本批统计 {events, risks_pushed, risks_failed}。
    """
    msg_ids = [record["id"] for record in batch_records]
    placeholders = ",".join("?" for _ in msg_ids)
    conn.execute(f"DELETE FROM events WHERE message_id IN ({placeholders})", msg_ids)

    stats = {"events": 0, "risks_pushed": 0, "risks_failed": 0}

    for record in batch_records:
        msg_id = record["id"]
        if msg_id in failed_ids:
            conn.execute(
                "UPDATE messages SET classified = 2, classify_error = ? WHERE id = ?",
                ("模型调用失败或输出不可解析，可手动重试", msg_id),
            )
            continue

        events = results.get(msg_id, [])
        for row in events:
            demand_id = None
            if row["event_type"] == "demand":
                demand_id = _register_demand(conn, row, record["created_at"])

            cursor = conn.execute(
                """
                INSERT INTO events
                (message_id, event_type, created_at, source_group, sender_key,
                 key_field, key_field2, value_text, payload, demand_id, risk_level)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    row["event_type"],
                    record["created_at"],
                    record["source_group"],
                    record["sender_key"],
                    row.get("key_field"),
                    row.get("key_field2"),
                    row.get("value_text"),
                    row.get("payload", "{}"),
                    demand_id,
                    row.get("risk_level"),
                ),
            )
            stats["events"] += 1

            if row["event_type"] == "risk" and row["risk_level"] in ("2", "3"):
                _handle_risk_push(conn, cursor.lastrowid, record, row, stats)

        conn.execute(
            "UPDATE messages SET classified = 1, classify_error = NULL WHERE id = ?",
            (msg_id,),
        )

    return stats


def _handle_risk_push(conn, event_id, record, row, stats):
    """二/三级风险推送。去重依据 messages.risk_pushed（事件重建不丢推送记录）。"""
    pushed_levels = {part for part in (record["risk_pushed"] or "").split(",") if part}
    level = row["risk_level"]

    if level in pushed_levels:
        push_status = "pushed"
    else:
        event_view = {
            "risk_level": level,
            "key_field": row.get("key_field"),
            "value_text": row.get("value_text"),
            "created_at": record["created_at"],
        }
        message_view = {
            "source_group": record["source_group"],
            "sender_name": record["sender_name"],
            "sender_key": record["sender_key"],
            "content": record["content"],
        }
        push_status, error = push_risk_alert(event_view, message_view)
        if push_status == "pushed":
            pushed_levels.add(level)
            stats["risks_pushed"] += 1
        elif push_status == "failed":
            stats["risks_failed"] += 1
            print(f"[风险推送失败] 消息 {record['id']} L{level}：{error}", flush=True)

    conn.execute(
        "UPDATE events SET push_status = ?, pushed_at = ? WHERE id = ?",
        (push_status, now_iso() if push_status == "pushed" else None, event_id),
    )
    if push_status == "pushed" and level not in (record["risk_pushed"] or "").split(","):
        conn.execute(
            "UPDATE messages SET risk_pushed = ? WHERE id = ?",
            (",".join(sorted(pushed_levels)), record["id"]),
        )


def _classify_batch(records, registry):
    """分类一批消息，返回 (results: {id: events}, failed_ids: set)。

    RetriableModelError（超时/网络/5xx）时拆半重发；解析失败标记整批失败。
    """
    try:
        text = call_model(
            build_user_prompt(records, registry),
            system=SYSTEM_PROMPT,
            max_tokens=classify_max_tokens(),
            temperature=0.1,
        )
        return parse_batch_output(text, [r["id"] for r in records]), set()
    except RetriableModelError as exc:
        print(f"[分类重试] 批次失败（{exc}），拆半重发", flush=True)
        if len(records) <= 10:
            return {}, {r["id"] for r in records}
        mid = len(records) // 2
        left_results, left_failed = _classify_batch(records[:mid], registry)
        right_results, right_failed = _classify_batch(records[mid:], registry)
        left_results.update(right_results)
        left_failed |= right_failed
        return left_results, left_failed
    except ValueError as exc:
        print(f"[分类失败] {exc}", flush=True)
        return {}, {r["id"] for r in records}


def classify_recent(days=None, limit=None, retry_failed=False, push_risks=True):
    """分类主入口：循环取待分类消息分批处理，返回统计 dict。"""
    load_env()
    init_db()

    batch_size = classify_batch_size()
    limit = min(max(int(limit or 5000), 1), 5000)
    stats = {
        "classified": 0,
        "failed": 0,
        "events": 0,
        "risks_pushed": 0,
        "risks_failed": 0,
        "batches": 0,
        "error": None,
    }
    total = 0

    while total < limit:
        with get_db() as conn:
            pending = fetch_pending(conn, days=days, limit=batch_size, retry_failed=retry_failed)
            if not pending:
                break
            registry = fetch_registry(conn)

        batch = rows_to_dicts(pending)
        results, failed_ids = _classify_batch(batch, registry)

        with get_db() as conn:
            batch_stats = store_batch_results(conn, batch, results, failed_ids, push_risks=push_risks)

        stats["batches"] += 1
        stats["classified"] += len(batch) - len(failed_ids)
        stats["failed"] += len(failed_ids)
        stats["events"] += batch_stats["events"]
        stats["risks_pushed"] += batch_stats["risks_pushed"]
        stats["risks_failed"] += batch_stats["risks_failed"]
        total += len(batch)
        print(
            f"[分类] 批次#{stats['batches']}：{len(batch)} 条，事件 {batch_stats['events']}，"
            f"失败 {len(failed_ids)}，风险推送 {batch_stats['risks_pushed']}",
            flush=True,
        )
        time.sleep(0.5)

    return stats
