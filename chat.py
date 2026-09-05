"""问答 agent 检索层：多表子串检索 + 图扩展 + 证据序列化 + 提示词。

知识库 = 原始群消息(messages) + 结构化事件(events) + 需求(demands)
       + AI 总结(summaries) + 运营报告(reports)。

关键点：报告/总结是 LLM 转述，措辞可能与原始消息不同（如「骑车」vs「踩车」），
因此先多表粗检索，再沿 需求→事件→消息 关联链扩展，还原原始出处。
"""

import json
from datetime import datetime, timedelta

# 提问中的常见引导词，不做检索候选
STOP_GRAMS = {
    "这是", "是从", "从哪", "哪个", "个群", "哪个人", "得到", "到的", "消息",
    "请问", "帮我", "帮我查", "查询", "一下", "什么", "谁", "的", "告诉我",
    "帮我找", "找出", "来自", "有没有", "哪些", "哪里", "多少", "为什么",
}
# 纯标点/空白字符（用于过滤候选）
_PUNCT = set("，。？！、,.?!;；:：·…—()（）[]【】{}<>《》\"'\"　 \t\r\n")

MAX_QUESTION_CHARS = 2000
HISTORY_TURNS = 10            # 带多少轮历史（每轮 user+assistant 两行）
HISTORY_MSG_CHAR_CAP = 1000   # 单条历史消息截断长度
EVIDENCE_MAX_ITEMS = 60
EVIDENCE_MAX_CHARS = 12000
CONTENT_CHAR_CAP = 300        # 单条消息/事件内容截断长度
EXCERPT_HALF = 120            # 总结/报告片段半窗

# 表优先级：消息最可信，总结最低（纯转述）
_PRIORITY = {"msg": 5, "event": 4, "demand": 4, "report": 3, "summary": 2}
# 最终证据按类型配额，防止高频消息挤掉溯源所需的报告/需求
_KIND_CAPS = {"msg": 25, "event": 12, "demand": 8, "report": 5, "summary": 5}
_NEIGHBOR_SECONDS = 180       # 邻近消息时间窗（同群 ±3 分钟）


def build_retrieval_candidates(question):
    """切句 → 每句全长(≤16字) + 3~8 字滑窗 → 过滤停用词/纯标点 → 去重，≤120 条。

    3 字起步：2 字子串（如「写歌」「直播」）在中文里噪音太大。
    """
    segments = []
    current = []
    for char in question:
        if char in _PUNCT:
            if current:
                segments.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        segments.append("".join(current))

    candidates = []
    seen = set()
    for segment in segments:
        if not segment:
            continue
        if len(segment) <= 16:
            candidates.append(segment)
            seen.add(segment)
        for size in range(3, min(8, len(segment)) + 1):
            for start in range(0, len(segment) - size + 1):
                gram = segment[start:start + size]
                if gram in seen:
                    continue
                if gram in STOP_GRAMS:
                    continue
                if all(char in _PUNCT for char in gram):
                    continue
                seen.add(gram)
                candidates.append(gram)
        if len(candidates) >= 120:
            break
    return candidates[:120]


def _like_pattern(gram):
    """转义 LIKE 通配符，配合 ESCAPE '\\' 使用。"""
    return gram.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _excerpt(content, position, half=EXCERPT_HALF):
    """围绕命中位置截取片段。"""
    if not content:
        return ""
    start = max(0, position - half)
    end = min(len(content), position + half)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return prefix + content[start:end].replace("\n", " ") + suffix


def _msg_query(where, params, limit=None):
    return f"""
        SELECT m.id, m.source_group, m.sender_name, m.sender_key, m.created_at, m.content,
               mc.chat_id
        FROM messages m
        LEFT JOIN monitored_chats mc ON mc.display_name = m.source_group
        WHERE {where}
        ORDER BY m.created_at DESC, m.id DESC
        {f"LIMIT {limit}" if limit else ""}
    """, params


def _event_query(where, params, limit=None):
    return f"""
        SELECT e.id, e.message_id, e.event_type, e.source_group, e.sender_key,
               e.key_field, e.key_field2, e.value_text, e.demand_id, e.risk_level,
               e.status, e.created_at, mc.chat_id, m.sender_name
        FROM events e
        LEFT JOIN monitored_chats mc ON mc.display_name = e.source_group
        LEFT JOIN messages m ON m.id = e.message_id
        WHERE {where}
        ORDER BY e.created_at DESC, e.id DESC
        {f"LIMIT {limit}" if limit else ""}
    """, params


def retrieve_evidence(question, conn):
    """多表检索 + 一层图扩展，返回 {"items": [...], "hit_stats": {...}}。"""
    candidates = build_retrieval_candidates(question)
    scores = {}     # (kind, id) -> {"score": int, "row": sqlite3.Row}
    msg_hits, event_hits, demand_hits = {}, {}, {}
    summary_hits, report_hits = {}, {}
    hits_seen = {"msg": 0, "event": 0, "demand": 0, "summary": 0, "report": 0}
    stop = set()

    def add(kind, row, gain, extra=None):
        key = (kind, row["id"])
        if key not in scores:
            scores[key] = {"score": 0, "row": row}
            if extra:
                scores[key].update(extra)
        scores[key]["score"] += gain

    for gram in candidates:
        pattern = f"%{_like_pattern(gram)}%"
        gain = len(gram)

        if "msg" not in stop:
            sql, params = _msg_query("m.content LIKE ? ESCAPE '\\'", (pattern,), 40)
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                add("msg", row, gain, extra={"direct": True})
                msg_hits[row["id"]] = True
            hits_seen["msg"] += len(rows)
            if hits_seen["msg"] >= 40:
                stop.add("msg")

        if "event" not in stop:
            sql, params = _event_query(
                "e.value_text LIKE ? ESCAPE '\\' OR e.key_field LIKE ? ESCAPE '\\'"
                " OR e.key_field2 LIKE ? ESCAPE '\\' OR e.payload LIKE ? ESCAPE '\\'",
                (pattern,) * 4, 40,
            )
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                add("event", row, gain)
                event_hits[row["id"]] = True
            hits_seen["event"] += len(rows)
            if hits_seen["event"] >= 40:
                stop.add("event")

        if "demand" not in stop:
            rows = conn.execute(
                "SELECT id, name, direction, first_seen, is_interesting, status, note"
                " FROM demands WHERE name LIKE ? ESCAPE '\\' OR COALESCE(note,'') LIKE ? ESCAPE '\\'"
                " ORDER BY id DESC LIMIT 30",
                (pattern, pattern),
            ).fetchall()
            for row in rows:
                add("demand", row, gain, extra={"direct": True})
                demand_hits[row["id"]] = True
            hits_seen["demand"] += len(rows)
            if hits_seen["demand"] >= 40:
                stop.add("demand")

        if "summary" not in stop:
            rows = conn.execute(
                "SELECT id, created_at, content FROM summaries"
                " WHERE content LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT 10",
                (pattern,),
            ).fetchall()
            for row in rows:
                position = row["content"].find(gram)
                add("summary", row, gain, extra={"position": position})
                summary_hits[row["id"]] = True
            hits_seen["summary"] += len(rows)
            if hits_seen["summary"] >= 40:
                stop.add("summary")

        if "report" not in stop:
            rows = conn.execute(
                "SELECT id, report_type, period_start, period_end, content, generated_at"
                " FROM reports WHERE content LIKE ? ESCAPE '\\' ORDER BY generated_at DESC LIMIT 10",
                (pattern,),
            ).fetchall()
            for row in rows:
                position = row["content"].find(gram)
                add("report", row, gain, extra={"position": position})
                report_hits[row["id"]] = True
            hits_seen["report"] += len(rows)
            if hits_seen["report"] >= 40:
                stop.add("report")

        if len(stop) == 5:
            break

    # ---- 邻近消息扩展：直接命中的消息同群 ±3 分钟内往往有完整上下文 ----
    # 按时间距离就近取 6 条，分数随距离衰减，不挤掉直接命中
    for mid in list(msg_hits):
        entry = scores[("msg", mid)]
        row = entry["row"]
        earliest = _shift_time(row["created_at"], -_NEIGHBOR_SECONDS)
        latest = _shift_time(row["created_at"], _NEIGHBOR_SECONDS)
        rows = conn.execute(
            """
            SELECT m.id, m.source_group, m.sender_name, m.sender_key, m.created_at, m.content,
                   mc.chat_id
            FROM messages m
            LEFT JOIN monitored_chats mc ON mc.display_name = m.source_group
            WHERE m.source_group = ? AND m.created_at BETWEEN ? AND ?
            ORDER BY ABS(julianday(m.created_at) - julianday(?))
            LIMIT 6
            """,
            (row["source_group"], earliest, latest, row["created_at"]),
        ).fetchall()
        for nrow in rows:
            key = ("msg", nrow["id"])
            if key in scores and scores[key].get("direct"):
                continue
            dist = _time_distance(row["created_at"], nrow["created_at"])
            score = max(1, entry["score"] - 1 - dist // 30)
            if key not in scores:
                scores[key] = {"score": 0, "row": nrow, "near": True}
            scores[key]["score"] = max(scores[key]["score"], score)

    # ---- 需求兄弟扩展：同批次注册的需求（first_seen ±60s）往往来自同一段对话 ----
    for did in list(demand_hits):
        entry = scores[("demand", did)]
        first_seen = entry["row"]["first_seen"]
        rows = conn.execute(
            "SELECT id, name, direction, first_seen, is_interesting, status, note"
            " FROM demands WHERE id != ? AND first_seen BETWEEN ? AND ? ORDER BY id",
            (did, _shift_time(first_seen, -60), _shift_time(first_seen, 60)),
        ).fetchall()
        for row in rows:
            key = ("demand", row["id"])
            if key in scores and scores[key].get("direct"):
                continue
            score = max(1, entry["score"] + 1)
            if key not in scores:
                scores[key] = {"score": 0, "row": row, "linked": True}
            scores[key]["score"] = max(scores[key]["score"], score)

    # ---- 图扩展：需求 → 事件 → 消息；事件 → 消息；事件 → 需求；邻近消息 → 事件 ----
    direct_demand_ids = set(demand_hits)
    direct_event_rows = [scores[("event", rid)]["row"] for rid in event_hits]
    # 含兄弟扩展进来的需求
    all_demand_ids = {key[1] for key in scores if key[0] == "demand"}
    demand_ids = sorted(all_demand_ids | {row["demand_id"] for row in direct_event_rows if row["demand_id"]})
    message_ids = sorted(set(msg_hits) | {row["message_id"] for row in direct_event_rows if row["message_id"]})
    # 邻近消息的 id 也纳入事件关联
    message_ids = sorted(
        set(message_ids) | {scores[key]["row"]["id"] for key in list(scores) if key[0] == "msg"}
    )

    linked_score = {}   # (kind, id) -> 关联来源得分（用于继承排序）
    if demand_ids:
        rows = conn.execute(
            f"SELECT e.id, e.message_id, e.event_type, e.source_group, e.sender_key,"
            f" e.key_field, e.key_field2, e.value_text, e.demand_id, e.risk_level,"
            f" e.status, e.created_at, mc.chat_id, m.sender_name"
            f" FROM events e"
            f" LEFT JOIN monitored_chats mc ON mc.display_name = e.source_group"
            f" LEFT JOIN messages m ON m.id = e.message_id"
            f" WHERE e.demand_id IN ({','.join('?' * len(demand_ids))})"
            f" AND e.status IN ('pending','confirmed') ORDER BY e.id",
            demand_ids,
        ).fetchall()
        for row in rows:
            source = scores.get(("demand", row["demand_id"]), {}).get("score", 0)
            key = ("event", row["id"])
            if key not in scores:
                scores[key] = {"score": 0, "row": row}
            scores[key]["score"] = max(scores[key]["score"], source + 1)
            linked_score[("msg", row["message_id"])] = max(
                linked_score.get(("msg", row["message_id"]), 0), source + 1)
            message_ids.append(row["message_id"])

    if direct_event_rows:
        demand_ids.extend(
            row["demand_id"] for row in direct_event_rows
            if row["demand_id"] and row["demand_id"] not in demand_ids)
        for row in direct_event_rows:
            if row["demand_id"]:
                linked_score[("demand", row["demand_id"])] = max(
                    linked_score.get(("demand", row["demand_id"]), 0),
                    scores[("event", row["id"])]["score"] + 1,
                )

    # 邻近消息 → 它们的事件（把邻接消息的完整分析也带进来）
    message_ids = sorted(set(message_ids))
    if message_ids:
        rows = conn.execute(
            f"SELECT e.id, e.message_id, e.event_type, e.source_group, e.sender_key,"
            f" e.key_field, e.key_field2, e.value_text, e.demand_id, e.risk_level,"
            f" e.status, e.created_at, mc.chat_id, m.sender_name"
            f" FROM events e"
            f" LEFT JOIN monitored_chats mc ON mc.display_name = e.source_group"
            f" LEFT JOIN messages m ON m.id = e.message_id"
            f" WHERE e.message_id IN ({','.join('?' * len(message_ids))}) ORDER BY e.id",
            message_ids,
        ).fetchall()
        for row in rows:
            source = scores.get(("msg", row["message_id"]), {}).get("score", 0)
            key = ("event", row["id"])
            if key not in scores:
                scores[key] = {"score": 0, "row": row}
            scores[key]["score"] = max(scores[key]["score"], source + 1)
            if row["demand_id"] and row["demand_id"] not in demand_ids:
                demand_ids.append(row["demand_id"])

    demand_ids = sorted(set(demand_ids))
    if demand_ids:
        rows = conn.execute(
            f"SELECT id, name, direction, first_seen, is_interesting, status, note FROM demands"
            f" WHERE id IN ({','.join('?' * len(demand_ids))})",
            demand_ids,
        ).fetchall()
        for row in rows:
            key = ("demand", row["id"])
            if key not in scores:
                scores[key] = {"score": linked_score.get(key, 1), "row": row}

    if message_ids:
        rows = conn.execute(
            _msg_query(f"m.id IN ({','.join('?' * len(message_ids))})", message_ids)[0],
            message_ids,
        ).fetchall()
        for row in rows:
            key = ("msg", row["id"])
            if key not in scores:
                scores[key] = {"score": linked_score.get(key, 1), "row": row}

    # ---- 按类型配额截断（同类内按 直接命中 > 近邻 > 得分/时间 排序）----
    by_kind = {}
    for (kind, _), entry in scores.items():
        row = entry["row"]
        keys = row.keys()
        time_key = (
            row["created_at"] if "created_at" in keys
            else row["first_seen"] if "first_seen" in keys
            else row["generated_at"] if "generated_at" in keys
            else ""
        )
        by_kind.setdefault(kind, []).append((entry, time_key))
    for kind in by_kind:
        by_kind[kind].sort(
            key=lambda pair: (pair[0].get("direct", 0), pair[0].get("near", 0), pair[0]["score"], pair[1]),
            reverse=True,
        )
        by_kind[kind] = by_kind[kind][:_KIND_CAPS.get(kind, 10)]

    final = []
    for kind in sorted(_PRIORITY, key=_PRIORITY.get, reverse=True):
        for entry, _ in by_kind.get(kind, []):
            row = entry["row"]
            position = entry.get("position") if kind in ("summary", "report") else None
            final.append(serialize_item(kind, row, position))
            if len(final) >= EVIDENCE_MAX_ITEMS:
                break
        if len(final) >= EVIDENCE_MAX_ITEMS:
            break

    return {
        "items": final,
        "hit_stats": {"candidates": len(candidates), "messages": hits_seen["msg"],
                      "events": hits_seen["event"], "demands": hits_seen["demand"],
                      "summaries": hits_seen["summary"], "reports": hits_seen["report"]},
    }


def _shift_time(value, seconds):
    """把 'YYYY-MM-DD HH:MM:SS' 平移秒数后格式化回同格式（用于邻近窗口）。"""
    if not value:
        return ""
    try:
        moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
    return (moment + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _time_distance(a, b):
    """两个 'YYYY-MM-DD HH:MM:SS' 字符串的秒差绝对值。"""
    if not a or not b:
        return 10**9
    try:
        da = datetime.strptime(a, "%Y-%m-%d %H:%M:%S")
        db = datetime.strptime(b, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 10**9
    return abs((da - db).total_seconds())


def serialize_item(kind, row, position=None):
    if kind == "msg":
        return {
            "id": f"msg#{row['id']}",
            "类型": "原始消息",
            "群名": row["source_group"],
            "群编号": row["chat_id"],
            "发送人": row["sender_name"],
            "发送人ID": row["sender_key"],
            "时间": row["created_at"],
            "内容": (row["content"] or "")[:CONTENT_CHAR_CAP],
        }
    if kind == "event":
        return {
            "id": f"event#{row['id']}",
            "类型": "结构化事件",
            "事件类型": row["event_type"],
            "关联消息ID": row["message_id"],
            "需求ID": row["demand_id"],
            "群名": row["source_group"],
            "群编号": row["chat_id"],
            "发送人": row["sender_name"],
            "发送人ID": row["sender_key"],
            "时间": row["created_at"],
            "内容": (row["value_text"] or row["key_field2"] or row["key_field"] or "")[:CONTENT_CHAR_CAP],
            "状态": row["status"],
        }
    if kind == "demand":
        return {
            "id": f"demand#{row['id']}",
            "类型": "需求",
            "需求名": row["name"],
            "方向": row["direction"],
            "首次出现": row["first_seen"],
            "状态": row["status"],
        }
    if kind == "summary":
        return {
            "id": f"summary#{row['id']}",
            "类型": "AI总结",
            "时间": row["created_at"],
            "相关片段": _excerpt(row["content"], position if position is not None else 0),
        }
    return {
        "id": f"report#{row['id']}",
        "类型": "周报" if row["report_type"] == "weekly" else "日报",
        "时段": f"{row['period_start']} ~ {row['period_end']}",
        "相关片段": _excerpt(row["content"], position if position is not None else 0),
    }


def serialize_evidence(items):
    """序列化证据，并严格保证最终 JSON 长度 ≤ EVIDENCE_MAX_CHARS（从尾部剔除）。"""
    while items:
        text = json.dumps(items, ensure_ascii=False, indent=2)
        if len(text) <= EVIDENCE_MAX_CHARS:
            return text
        items = items[:-1]
    return "[]"


def build_chat_system_prompt(evidence_json):
    return f"""你是 Muse AI 社群客服系统的「问答」助手，用中文回答运营/客服的问题。

你的知识库是企业微信社群采集数据：原始群聊消息、AI 结构化事件、用户需求、AI 总结和运营报告。
回答必须严格遵守：

1. 只依据下方【证据】回答，证据里没有的信息一律不得编造、推测或补充。
   证据不足以回答时，直接说明「未找到相关信息」，并简要说明检索了哪些来源（消息/事件/需求/总结/报告）。
2. 回答溯源类问题（例如"这条消息来自哪个群哪个人"）时，逐条引用证据，每条引用必须给出：
   群名、群编号（chat_id，缺失时写「群编号未同步」）、发送人、发送时间、以及消息/事件原文（用引号括起）。
3. 证据中若有多条相似消息（不同群、不同人、不同时间），必须全部列出并按时间先后说明，不得只挑一条。
   此外，证据里同一发送人在同一时段（±3 分钟内）的其他相关消息也要一并列出——它们通常属于同一段对话，都是溯源的一部分。
4. 每条引用末尾附上证据 id（如 msg#173、event#81、demand#2、report#2），便于人工核对。
5. 证据可能来自 AI 总结或报告的转述，措辞可能与原始消息不同（例如提问说「骑车」而原消息写「踩车」）。
   当提问措辞与原始消息不一致时，必须明确指出该措辞出自哪条报告/总结（附其证据 id，如 report#2），
   并说明你据此把提问映射到了哪条原始消息。若证据中的报告/总结片段包含提问措辞，不得跳过这条定位说明。
6. 输出 markdown，中文，简洁；涉及多条消息时优先使用列表或表格。
7. 回答不要重复罗列整段证据 JSON。

【证据】
{evidence_json}"""


def build_llm_messages(history_rows, question, evidence_json):
    """组装多轮消息：system(含证据) + 截断后的最近历史 + 当前提问。"""
    messages = [{"role": "system", "content": build_chat_system_prompt(evidence_json)}]
    for row in history_rows:
        content = str(row["content"] or "")
        if len(content) > HISTORY_MSG_CHAR_CAP:
            content = content[:HISTORY_MSG_CHAR_CAP] + "…"
        messages.append({"role": row["role"], "content": content})
    messages.append({"role": "user", "content": question})
    return messages


def canned_no_evidence():
    return (
        "未找到相关信息。已检索原始消息、AI 结构化事件、需求、AI 总结和运营报告，"
        "均没有与你的问题匹配的内容。可以换个关键词试试。"
    )
