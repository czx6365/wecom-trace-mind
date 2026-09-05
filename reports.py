"""日报/周报生成：周期计算 → SQL 聚合 → LLM 写措辞 → 入库/推送。

幂等：reports 表 UNIQUE(report_type, period_start)，INSERT OR IGNORE 占位防并发；
生成异常时删除占位行，允许下次重试。
"""

import json
import os
from datetime import datetime, timedelta

from .db import get_db, init_db, load_env, now_iso, rows_to_dicts
from .llm import call_model
from .stats import collect_stats
from .wecom import push_wechat_markdown


def period_range(report_type, date_str=None):
    """返回 (start, end) 本地时间字符串。日报=当日；周报=本周一~周日。"""
    if date_str:
        day = datetime.strptime(date_str, "%Y-%m-%d")
    else:
        day = datetime.now()
    if report_type == "daily":
        return (
            day.strftime("%Y-%m-%d 00:00:00"),
            day.strftime("%Y-%m-%d 23:59:59"),
        )
    monday = day - timedelta(days=day.weekday())
    return (
        monday.strftime("%Y-%m-%d 00:00:00"),
        (monday + timedelta(days=6)).strftime("%Y-%m-%d 23:59:59"),
    )


def build_report_prompt(stats, report_type):
    """把聚合数字交给 LLM：只写措辞，禁止编造/修改数字。"""
    numbers = json.dumps(stats, ensure_ascii=False, indent=2)

    if report_type == "daily":
        sections = (
            "1. 风险事件：按三级/二级/一级列明类别与数量，附具体事件（群/发送者/内容摘要）；"
            "标注哪些已推送、哪些待人工确认。"
            "2. 异常爆发问题：列出异常增长的功能反馈（如有），没有写「无」。"
            "3. 当日新需求：列出首次出现的需求（如有），没有写「无」。"
            "4. 负面情绪来源：列出负面情绪主要来源（如有），没有写「无」。"
            "5. 一句话概览：消息总量、活跃用户数。"
        )
    else:
        sections = (
            "1. 概览：消息总量、活跃用户数、风险事件汇总。"
            "2. 功能反馈 TOP：按计数从高到低列出，附 1-2 个问题样本。"
            "3. 新需求 TOP：按计数从高到低列出，标注首次出现时间、7天/30天计数、"
            "增长最快需求（含上周期计数对比）。"
            "4. 曲风需求趋势：本周期 vs 上周期计数与环比百分比（pct 为 null 表示新增曲风，写「新」）。"
            "5. 社群情绪分布：正面/中性/负面数量与占比、主要负面来源。"
            "6. 小众但有趣的需求：列出（如有），没有写「无」。"
            "7. 建议下周验证的 Top 3：基于以上数据给出 3 条以内建议。"
        )

    return f"""你是 Muse AI 社群的运营分析助手。请基于给定的统计数据写一份中文 {('日报' if report_type == 'daily' else '周报')}，输出 markdown。

统计周期：{stats['start'][:10]} ~ {stats['end'][:10]}

硬性要求：
1. 只允许使用下面统计数据里出现的数字，禁止编造、修改、估算任何数字；数据里没有的写「无」。
2. 只写措辞和解读，不要重复罗列原始 JSON。
3. 输出结构按以下板块顺序：
{sections}

统计数据：
{numbers}
""".strip()


def generate_report(report_type, date_str=None, force=False, push=False, mark_done=False):
    """生成一份日报/周报。返回 dict：{ok, report_id, content, pushed, error}。"""
    load_env()
    init_db()

    start, end = period_range(report_type, date_str)
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM reports WHERE report_type = ? AND period_start = ?",
            (report_type, start),
        ).fetchone()
        if existing and not force:
            if mark_done:
                from .db import set_setting

                if report_type == "daily":
                    set_setting("daily_report_date", start[:10])
                else:
                    set_setting("weekly_report_week", start[:10])
            return {"ok": False, "message": f"该周期报告已存在（#{existing['id']}），--force 可重新生成"}

        placeholder_id = None
        if existing:
            placeholder_id = existing["id"]
        else:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO reports
                (report_type, period_start, period_end, content, stats_json, generated_at)
                VALUES (?, ?, ?, '', '{}', ?)
                """,
                (report_type, start, end, now_iso()),
            )
            if cursor.lastrowid:
                placeholder_id = cursor.lastrowid
            else:
                row = conn.execute(
                    "SELECT id FROM reports WHERE report_type = ? AND period_start = ?",
                    (report_type, start),
                ).fetchone()
                placeholder_id = row["id"] if row else None

        stats = collect_stats(conn, report_type, start, end)

    try:
        content = call_model(build_report_prompt(stats, report_type), max_tokens=8000)
    except Exception as exc:
        if placeholder_id:
            with get_db() as conn:
                conn.execute(
                    "DELETE FROM reports WHERE id = ? AND content = ''", (placeholder_id,)
                )
        return {"ok": False, "message": f"报告生成失败：{exc}"}

    pushed = False
    push_error = None
    if push:
        try:
            pushed = push_wechat_markdown(content)
        except Exception as exc:
            push_error = str(exc)

    with get_db() as conn:
        conn.execute(
            """
            UPDATE reports SET content = ?, stats_json = ?, generated_at = ?,
                pushed_to_wechat = ?, push_error = ?
            WHERE id = ?
            """,
            (
                content,
                json.dumps(stats, ensure_ascii=False),
                now_iso(),
                1 if pushed else 0,
                push_error,
                placeholder_id,
            ),
        )

    if mark_done:
        from .db import set_setting

        if report_type == "daily":
            set_setting("daily_report_date", start[:10])
        else:
            set_setting("weekly_report_week", start[:10])

    return {
        "ok": True,
        "report_id": placeholder_id,
        "period_start": start,
        "period_end": end,
        "content": content,
        "pushed": pushed,
        "push_error": push_error,
    }


def list_reports(report_type=None, start_date=None, end_date=None, limit=50):
    with get_db() as conn:
        sql = "SELECT * FROM reports"
        clauses, params = [], []
        if report_type:
            clauses.append("report_type = ?")
            params.append(report_type)
        if start_date:
            clauses.append("period_end >= ?")
            params.append(f"{start_date} 00:00:00")
        if end_date:
            clauses.append("period_start <= ?")
            params.append(f"{end_date} 23:59:59")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY period_start DESC, id DESC LIMIT ?"
        params.append(min(max(int(limit), 1), 200))
        rows = conn.execute(sql, params).fetchall()
    return rows_to_dicts(rows)
