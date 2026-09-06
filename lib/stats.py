"""SQL 聚合层：报表/面板所有数字都在这里算（纯 dict 输出）。

原则：LLM 不报数，只拿这里的数字写措辞。
时间均为本地时间字符串（YYYY-MM-DD HH:MM:SS），闭区间 BETWEEN 比较。
统计口径：events.status IN ('pending','confirmed')；消息统计排除噪音。
"""

from datetime import datetime, timedelta

from .db import get_db, rows_to_dicts

STATUS_SQL = "status IN ('pending','confirmed')"


def count_messages(conn, start, end):
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM messages
        WHERE created_at BETWEEN ? AND ? AND is_noise = 0
        """,
        (start, end),
    ).fetchone()
    return row["cnt"]


def active_users(conn, start, end):
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT sender_key) AS cnt FROM messages
        WHERE created_at BETWEEN ? AND ? AND is_noise = 0 AND sender_key != 'unknown'
        """,
        (start, end),
    ).fetchone()
    return row["cnt"]


def risk_summary(conn, start, end):
    """按 等级 × 类别 汇总风险事件数。"""
    rows = conn.execute(
        f"""
        SELECT risk_level, key_field AS category, COUNT(*) AS cnt
        FROM events
        WHERE event_type = 'risk' AND {STATUS_SQL} AND created_at BETWEEN ? AND ?
        GROUP BY risk_level, key_field
        ORDER BY risk_level DESC, cnt DESC
        """,
        (start, end),
    ).fetchall()
    return rows_to_dicts(rows)


def risk_events_detail(conn, start, end, limit=100):
    """风险事件明细（报告里列具体内容用）。"""
    rows = conn.execute(
        f"""
        SELECT e.id, e.risk_level, e.key_field AS category, e.value_text AS detail,
               e.created_at, e.source_group, m.sender_name, m.content,
               e.push_status, e.status
        FROM events e JOIN messages m ON m.id = e.message_id
        WHERE e.event_type = 'risk' AND e.status IN ('pending','confirmed')
          AND e.created_at BETWEEN ? AND ?
        ORDER BY e.risk_level DESC, e.created_at DESC
        LIMIT ?
        """,
        (start, end, limit),
    ).fetchall()
    return rows_to_dicts(rows)


def feedback_top(conn, start, end, limit=10):
    """功能反馈 TOP：按功能名计数 + 每条功能最近 3 个问题样本。"""
    rows = conn.execute(
        f"""
        SELECT key_field AS feature, COUNT(*) AS cnt
        FROM events
        WHERE event_type = 'feedback' AND {STATUS_SQL} AND created_at BETWEEN ? AND ?
        GROUP BY key_field
        ORDER BY cnt DESC, key_field ASC
        LIMIT ?
        """,
        (start, end, limit),
    ).fetchall()
    top = rows_to_dicts(rows)
    for item in top:
        samples = conn.execute(
            f"""
            SELECT value_text FROM events
            WHERE event_type = 'feedback' AND {STATUS_SQL}
              AND created_at BETWEEN ? AND ? AND key_field = ? AND value_text != ''
            ORDER BY created_at DESC LIMIT 3
            """,
            (start, end, item["feature"]),
        ).fetchall()
        item["samples"] = [row["value_text"] for row in samples]
    return top


def demand_counts(conn, start, end):
    """周期内各需求（按注册表）事件计数。"""
    rows = conn.execute(
        f"""
        SELECT d.id, d.name, d.direction, d.first_seen, COUNT(*) AS cnt
        FROM events e JOIN demands d ON e.demand_id = d.id
        WHERE e.event_type = 'demand' AND e.{STATUS_SQL} AND e.created_at BETWEEN ? AND ?
        GROUP BY d.id
        """,
        (start, end),
    ).fetchall()
    return rows_to_dicts(rows)


def demand_top(conn, start, end, limit=10):
    counts = demand_counts(conn, start, end)
    counts.sort(key=lambda item: (-item["cnt"], item["name"]))
    top = counts[:limit]
    now = datetime.now()
    for item in top:
        for days, key in ((7, "cnt_7d"), (30, "cnt_30d")):
            since = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt FROM events
                WHERE event_type = 'demand' AND {STATUS_SQL} AND demand_id = ? AND created_at >= ?
                """,
                (item["id"], since),
            ).fetchone()
            item[key] = row["cnt"]
    return top


def demand_growth(conn, start, end, limit=3):
    """增长最快需求：本周期计数 - 上周期计数（上周期为 0 也参与排序）。"""
    start_dt = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    end_dt = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
    span = end_dt - start_dt
    prev_start = (start_dt - span - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    prev_end = (start_dt - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

    this = {item["id"]: item for item in demand_counts(conn, start, end)}
    prev = {item["id"]: item["cnt"] for item in demand_counts(conn, prev_start, prev_end)}

    growth = []
    for demand_id, item in this.items():
        item["cnt_last"] = prev.get(demand_id, 0)
        item["growth"] = item["cnt"] - item["cnt_last"]
        growth.append(item)
    growth.sort(key=lambda item: (-item["growth"], -item["cnt"]))
    return growth[:limit]


def style_trend(conn, start, end):
    """曲风本周期 vs 上周期计数与环比%（last=0 标 NEW，pct 在这里算好）。"""
    start_dt = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    end_dt = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
    span = end_dt - start_dt
    prev_start = (start_dt - span - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    prev_end = (start_dt - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")

    def _counts(s, e):
        rows = conn.execute(
            f"""
            SELECT key_field AS style, COUNT(*) AS cnt
            FROM events
            WHERE event_type = 'style' AND {STATUS_SQL} AND created_at BETWEEN ? AND ?
            GROUP BY key_field
            """,
            (s, e),
        ).fetchall()
        return {row["style"]: row["cnt"] for row in rows}

    this = _counts(start, end)
    last = _counts(prev_start, prev_end)
    styles = sorted(set(this) | set(last))
    result = []
    for style in styles:
        cnt_this = this.get(style, 0)
        cnt_last = last.get(style, 0)
        if cnt_last > 0:
            pct = round((cnt_this - cnt_last) * 100.0 / cnt_last)
        else:
            pct = None  # None = 新增（NEW）
        result.append({"style": style, "cnt_this": cnt_this, "cnt_last": cnt_last, "pct": pct})
    result.sort(key=lambda item: -item["cnt_this"])
    return result


def sentiment_dist(conn, start, end):
    """情绪分布 + 负面来源 TOP5。"""
    rows = conn.execute(
        f"""
        SELECT key_field AS emotion, COUNT(*) AS cnt
        FROM events
        WHERE event_type = 'sentiment' AND {STATUS_SQL} AND created_at BETWEEN ? AND ?
        GROUP BY key_field
        """,
        (start, end),
    ).fetchall()
    dist = {"positive": 0, "neutral": 0, "negative": 0}
    for row in rows:
        if row["emotion"] in dist:
            dist[row["emotion"]] = row["cnt"]

    sources = conn.execute(
        f"""
        SELECT value_text AS reason, COUNT(*) AS cnt
        FROM events
        WHERE event_type = 'sentiment' AND {STATUS_SQL}
          AND key_field = 'negative' AND value_text != '' AND created_at BETWEEN ? AND ?
        GROUP BY value_text
        ORDER BY cnt DESC LIMIT 5
        """,
        (start, end),
    ).fetchall()
    dist["sources"] = rows_to_dicts(sources)
    return dist


def daily_spikes(conn, start, end):
    """异常爆发：当日某功能反馈数 ≥3 且 ≥ 前 7 天日均 ×2。"""
    start_dt = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    prev_end = (start_dt - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    prev_start = (start_dt - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        f"""
        SELECT key_field AS feature, COUNT(*) AS cnt
        FROM events
        WHERE event_type = 'feedback' AND {STATUS_SQL} AND created_at BETWEEN ? AND ?
        GROUP BY key_field
        """,
        (start, end),
    ).fetchall()

    spikes = []
    for row in rows:
        if row["cnt"] < 3:
            continue
        prev = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt FROM events
            WHERE event_type = 'feedback' AND {STATUS_SQL}
              AND key_field = ? AND created_at BETWEEN ? AND ?
            """,
            (row["feature"], prev_start, prev_end),
        ).fetchone()
        daily_avg = round(prev["cnt"] / 7.0, 1)
        if row["cnt"] >= daily_avg * 2:
            spikes.append({"feature": row["feature"], "cnt_today": row["cnt"], "daily_avg": daily_avg})
    spikes.sort(key=lambda item: -item["cnt_today"])
    return spikes


def new_demands_today(conn, start, end):
    """首次出现在本周期的新需求。"""
    rows = conn.execute(
        """
        SELECT id, name, direction, first_seen, is_interesting
        FROM demands
        WHERE status = 'active' AND first_seen BETWEEN ? AND ?
        ORDER BY first_seen DESC
        """,
        (start, end),
    ).fetchall()
    return rows_to_dicts(rows)


def interesting_demands(conn, start, end):
    """本周期内有人提过的小众但有趣需求。"""
    rows = conn.execute(
        f"""
        SELECT d.id, d.name, d.direction, d.first_seen, COUNT(*) AS cnt
        FROM events e JOIN demands d ON e.demand_id = d.id
        WHERE e.event_type = 'demand' AND e.{STATUS_SQL} AND e.created_at BETWEEN ? AND ?
          AND d.is_interesting = 1
        GROUP BY d.id
        ORDER BY cnt DESC
        """,
        (start, end),
    ).fetchall()
    return rows_to_dicts(rows)


def collect_stats(conn, report_type, start, end):
    """按报告类型收集全部聚合数字。"""
    stats = {
        "report_type": report_type,
        "start": start,
        "end": end,
        "messages": count_messages(conn, start, end),
        "active_users": active_users(conn, start, end),
        "risks": risk_summary(conn, start, end),
        "risk_details": risk_events_detail(conn, start, end),
    }

    if report_type == "daily":
        stats["spikes"] = daily_spikes(conn, start, end)
        stats["new_demands"] = new_demands_today(conn, start, end)
        stats["interesting"] = interesting_demands(conn, start, end)
        stats["sentiment"] = sentiment_dist(conn, start, end)
    else:
        stats["feedback_top"] = feedback_top(conn, start, end)
        stats["demand_top"] = demand_top(conn, start, end)
        stats["demand_growth"] = demand_growth(conn, start, end)
        stats["style_trend"] = style_trend(conn, start, end)
        stats["sentiment"] = sentiment_dist(conn, start, end)
        stats["new_demands"] = new_demands_today(conn, start, end)
        stats["interesting"] = interesting_demands(conn, start, end)
    return stats


def panel_stats(days=7):
    """新面板顶部统计条。"""
    with get_db() as conn:
        now = datetime.now()
        today_start = now.strftime("%Y-%m-%d 00:00:00")
        today_end = now.strftime("%Y-%m-%d 23:59:59")
        week_start = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00:00")

        pending_classify = conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages WHERE is_noise = 0 AND classified IN (0, 2)"
        ).fetchone()["cnt"]
        pending_confirm = conn.execute(
            "SELECT COUNT(*) AS cnt FROM events WHERE status = 'pending'"
        ).fetchone()["cnt"]
        active_demands = conn.execute(
            "SELECT COUNT(*) AS cnt FROM demands WHERE status = 'active'"
        ).fetchone()["cnt"]

        risk_counts = {"1": 0, "2": 0, "3": 0}
        for row in conn.execute(
            f"""
            SELECT risk_level, COUNT(*) AS cnt FROM events
            WHERE event_type = 'risk' AND {STATUS_SQL} AND created_at BETWEEN ? AND ?
            GROUP BY risk_level
            """,
            (week_start, today_end),
        ).fetchall():
            if row["risk_level"] in risk_counts:
                risk_counts[row["risk_level"]] = row["cnt"]

        return {
            "today_messages": count_messages(conn, today_start, today_end),
            "week_messages": count_messages(conn, week_start, today_end),
            "today_users": active_users(conn, today_start, today_end),
            "week_users": active_users(conn, week_start, today_end),
            "pending_classify": pending_classify,
            "pending_confirm": pending_confirm,
            "active_demands": active_demands,
            "risk_counts": risk_counts,
        }
