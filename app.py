import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lib.classifier import classify_recent
from lib.chat import (
    HISTORY_TURNS,
    MAX_QUESTION_CHARS,
    build_llm_messages,
    canned_no_evidence,
    retrieve_evidence,
    serialize_evidence,
)
from lib.db import (
    BASE_DIR,
    PUBLIC_DIR,
    RUNTIME_PATH,
    get_db,
    get_setting,
    init_db,
    load_env,
    now_iso,
    rows_to_dicts,
    set_setting,
)
from lib.llm import RetriableModelError, call_model, call_model_stream
from lib.reports import generate_report as generate_report_core
from lib.reports import list_reports
from lib.stats import panel_stats
from lib.wecom import push_wechat_markdown, push_risk_event


def json_response(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------- 首页 AI 总结流程


def build_summary_prompt(messages, events, group_names=None):
    message_items = []
    for item in messages:
        message_items.append(
            {
                "id": item["id"],
                "time": item["created_at"],
                "source_group": item["source_group"],
                "sender": item["sender_name"] or item["sender_key"],
                "content": item["content"],
            }
        )

    event_items = []
    for item in events:
        event_items.append(
            {
                "id": item["id"],
                "message_id": item["message_id"],
                "time": item["created_at"],
                "source_group": item["source_group"],
                "type": item["event_type"],
                "risk_level": item["risk_level"],
                "key": item["key_field"],
                "key2": item["key_field2"],
                "value": item["value_text"],
                "status": item["status"],
            }
        )

    scope = "、".join(group_names or []) if group_names else "全部已采集群"
    if messages:
        period = f"{messages[0]['created_at']} 至 {messages[-1]['created_at']}"
    else:
        period = "暂无"
    return f"""
你是 Muse AI 社群客服与用户洞察助手。请基于下面的社群消息和 AI 结构化事件，输出一份可直接给运营、客服和产品看的中文总结。

总结范围：{scope}
数据时段：{period}

要求：
1. 标题固定为“# Muse AI 社群 AI 总结”。
2. 先写统计范围和数据时段。
3. 按「风险与需人工关注」「用户主要疑问」「功能反馈」「新需求」「曲风/模板趋势」「发行反馈与资讯」「社群情绪与创作热度」组织内容。
4. 合并重复或相似问题，不要逐条复述聊天记录。
5. 对重要事项标注建议优先级：P1/P2/P3。
6. 如果涉及多个群，请指出跨群共性问题；只来自单个群时写明群名。
7. 最后给出 3 条以内运营/产品下一步动作建议。

AI 结构化事件：
{json.dumps(event_items, ensure_ascii=False, indent=2)}

原始消息样本：
{json.dumps(message_items, ensure_ascii=False, indent=2)}
""".strip()


def create_summary(send_to_wechat=False, mode="pending", limit=100, group_names=None):
    group_names = [name for name in (group_names or []) if name]
    limit = min(max(int(limit or 100), 1), 500)
    with get_db() as conn:
        values = []
        where = "WHERE is_noise = 0"
        if group_names:
            placeholders = ",".join("?" for _ in group_names)
            where += f" AND source_group IN ({placeholders})"
            values.extend(group_names)
        values.append(limit)
        messages = rows_to_dicts(
            conn.execute(
                f"""
                SELECT id, created_at, source_group, sender_name, sender_key, content
                FROM messages
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                values,
            ).fetchall()
        )
        messages.reverse()

        if messages:
            message_ids = [item["id"] for item in messages]
            placeholders = ",".join("?" for _ in message_ids)
            events = rows_to_dicts(
                conn.execute(
                    f"""
                    SELECT id, message_id, event_type, created_at, source_group, key_field,
                           key_field2, value_text, risk_level, status
                    FROM events
                    WHERE message_id IN ({placeholders})
                      AND status IN ('pending','confirmed')
                    ORDER BY created_at ASC, id ASC
                    """,
                    message_ids,
                ).fetchall()
            )
        else:
            events = []

    if not messages:
        return {"ok": False, "message": "没有可总结的社群消息"}

    prompt = build_summary_prompt(messages, events, group_names=group_names)
    summary = call_model(prompt)
    if not summary.lstrip().startswith("# Muse AI 社群 AI 总结"):
        summary = "# Muse AI 社群 AI 总结\n\n" + summary.lstrip()
    pushed = False
    error = None

    if send_to_wechat:
        try:
            pushed = push_wechat_markdown(summary)
        except Exception as exc:
            error = str(exc)

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO summaries (created_at, feedback_count, content, pushed_to_wechat, error)
            VALUES (?, ?, ?, ?, ?)
            """,
            (now_iso(), len(messages), summary, 1 if pushed else 0, error),
        )
        summary_id = cursor.lastrowid

    return {
        "ok": True,
        "summary_id": summary_id,
        "feedback_count": len(messages),
        "summary": summary,
        "pushed_to_wechat": pushed,
        "push_error": error,
    }


# ---------------------------------------------------------------- 采集/调度


def env_value(key, default=""):
    return os.environ.get(key, default).strip()


def run_json_command(command, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(detail or f"命令失败：{' '.join(command)}")
    return json.loads(result.stdout)


def wecom_command(*args):
    python_bin = env_value("WECOM_PYTHON", "python3")
    wecom_pro = env_value("WECOM_PRO_PATH")
    if not wecom_pro:
        raise RuntimeError("请先在 .env 里填写 WECOM_PRO_PATH")
    return [python_bin, wecom_pro, *args]


def wecom_data_args():
    data_dir = env_value("WECOM_DATA_DIR")
    return ["--data-dir", data_dir] if data_dir else []


def selected_group_names():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT display_name FROM monitored_chats WHERE enabled = 1 ORDER BY display_name"
        ).fetchall()
    return [row["display_name"] for row in rows]


def get_auto_interval_minutes():
    try:
        return max(int(get_setting("auto_interval_minutes", "0") or 0), 0)
    except (TypeError, ValueError):
        return 0


def spawn_import_once(quiet=True):
    script = BASE_DIR / "scripts" / "wecom_auto_import.py"
    kwargs = {"cwd": str(BASE_DIR)}
    if quiet:
        kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.Popen([sys.executable, str(script), "--once"], **kwargs)


def parse_setting_time(value):
    if not value:
        return 0.0
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0.0


def scheduler_loop():
    """按网页设置的间隔自动采集所选群最新消息；脚本导入到新消息后会自动触发分类。"""
    # 从上次采集时间继续计时，避免每次重启服务都立刻跑一轮
    last_run = parse_setting_time(get_setting("last_auto_import_at")) or time.time()
    proc = None
    while True:
        interval = get_auto_interval_minutes()
        if interval <= 0:
            last_run = 0.0
            time.sleep(30)
            continue

        # 上一轮还没跑完就跳过，避免两个采集进程同时解密
        if proc is not None and proc.poll() is None:
            time.sleep(30)
            continue

        if last_run and time.time() - last_run < interval * 60:
            time.sleep(30)
            continue

        try:
            proc = spawn_import_once(quiet=False)
            last_run = time.time()
            set_setting("last_auto_import_at", now_iso())
            print(f"[自动采集] {now_iso()} 开始一轮采集，间隔 {interval} 分钟", flush=True)
        except Exception:
            traceback.print_exc()
            last_run = time.time()
        time.sleep(30)


def report_scheduler_loop():
    """到点生成日报（默认 9 点，覆盖昨日）；周一补上周周报。

    子进程（scripts/run_report.py --mark-done）成功后写入 settings 防重复。
    """
    proc = None
    while True:
        try:
            if proc is not None and proc.poll() is None:
                time.sleep(30)
                continue

            try:
                hour = int(env_value("REPORT_DAILY_HOUR", "9") or "9")
            except ValueError:
                hour = 9
            now = datetime.now()
            if now.hour < hour:
                time.sleep(30)
                continue

            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            commands = []
            if get_setting("daily_report_date") != yesterday:
                cmd = [
                    sys.executable,
                    str(BASE_DIR / "scripts" / "run_report.py"),
                    "--type", "daily", "--date", yesterday, "--mark-done",
                ]
                if env_value("AUTO_PUSH_DAILY", "false").lower() == "true":
                    cmd.append("--push")
                commands.append(cmd)

            if now.weekday() == 0:
                last_monday = (now - timedelta(days=7)).strftime("%Y-%m-%d")
                if get_setting("weekly_report_week") != last_monday:
                    cmd = [
                        sys.executable,
                        str(BASE_DIR / "scripts" / "run_report.py"),
                        "--type", "weekly", "--date", last_monday, "--mark-done",
                    ]
                    if env_value("AUTO_PUSH_WEEKLY", "false").lower() == "true":
                        cmd.append("--push")
                    commands.append(cmd)

            for cmd in commands:
                proc = subprocess.Popen(cmd, cwd=str(BASE_DIR))
                print(f"[定时报告] {now_iso()} 启动：{' '.join(cmd)}", flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(30)


def run_classify_async(payload):
    """后台线程跑分类管线，settings 防重入。"""
    if get_setting("classify_running") == "1":
        return {"ok": False, "message": "分类任务正在进行中，请稍后查看状态"}

    set_setting("classify_running", "1")
    set_setting(
        "classify_last_result",
        json.dumps({"running": True, "started_at": now_iso()}, ensure_ascii=False),
    )

    days = payload.get("days")
    if days is not None:
        days = int(days)
    else:
        # 未指定时按 CLASSIFY_DAYS 窗口（默认 7 天），避免把全量历史都丢给模型
        days = int(env_value("CLASSIFY_DAYS", "7") or "7")
    limit = int(payload.get("limit") or 5000)
    retry_failed = bool(payload.get("retry_failed"))

    def worker():
        try:
            stats = classify_recent(days=days, limit=limit, retry_failed=retry_failed)
            stats["running"] = False
            stats["finished_at"] = now_iso()
            set_setting("classify_last_result", json.dumps(stats, ensure_ascii=False))
        except Exception as exc:
            traceback.print_exc()
            set_setting(
                "classify_last_result",
                json.dumps(
                    {"running": False, "error": str(exc), "finished_at": now_iso()},
                    ensure_ascii=False,
                ),
            )
        finally:
            set_setting("classify_running", "0")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "started": True, "message": "分类任务已启动，可在消息流页查看进度"}


# ---------------------------------------------------------------- HTTP 处理


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.serve_static("index.html", "text/html; charset=utf-8")
            return

        if path == "/panel":
            self.serve_static("panel.html", "text/html; charset=utf-8")
            return

        if path == "/api/feedbacks":
            self.handle_list_feedbacks(parsed.query)
            return

        if path == "/api/summaries":
            self.handle_list_summaries(parsed.query)
            return

        if path == "/api/groups":
            self.handle_list_groups()
            return

        if path == "/api/settings":
            self.handle_get_settings()
            return

        if path == "/api/messages":
            self.handle_list_messages(parsed.query)
            return

        if path == "/api/events":
            self.handle_list_events(parsed.query)
            return

        if path == "/api/classify/status":
            self.handle_classify_status()
            return

        if path == "/api/demands":
            self.handle_list_demands()
            return

        if path == "/api/reports":
            self.handle_list_reports(parsed.query)
            return

        if path == "/api/panel/stats":
            self.handle_panel_stats(parsed.query)
            return

        if path == "/api/chat/history":
            self.handle_chat_history()
            return

        if path.startswith("/public/"):
            filename = path.removeprefix("/public/")
            content_type = "text/plain; charset=utf-8"
            if filename.endswith(".css"):
                content_type = "text/css; charset=utf-8"
            elif filename.endswith(".js"):
                content_type = "application/javascript; charset=utf-8"
            self.serve_static(filename, content_type)
            return

        json_response(self, {"error": "Not found"}, status=404)

    def do_POST(self):
        parsed = urlparse(self.path)

        try:
            if parsed.path == "/api/summaries/run":
                self.handle_run_summary()
                return
            if parsed.path == "/api/groups/sync":
                self.handle_sync_groups()
                return
            if parsed.path == "/api/groups/update":
                self.handle_update_groups()
                return
            if parsed.path == "/api/settings":
                self.handle_update_settings()
                return
            if parsed.path == "/api/wecom/import-once":
                self.handle_import_once()
                return
            if parsed.path == "/api/feedbacks/close":
                self.handle_close_feedback()
                return
            if parsed.path == "/api/classify/run":
                self.handle_run_classify()
                return
            if parsed.path == "/api/events/confirm":
                self.handle_confirm_events()
                return
            if parsed.path == "/api/events/repush":
                self.handle_repush_events()
                return
            if parsed.path == "/api/demands/update":
                self.handle_update_demand()
                return
            if parsed.path == "/api/reports/run":
                self.handle_run_report()
                return
            if parsed.path == "/api/chat":
                self.handle_chat_stream()
                return
            if parsed.path == "/api/chat/clear":
                self.handle_chat_clear()
                return

            json_response(self, {"error": "Not found"}, status=404)
        except Exception as exc:
            traceback.print_exc()
            json_response(self, {"error": str(exc)}, status=500)

    def serve_static(self, filename, content_type):
        file_path = (PUBLIC_DIR / filename).resolve()
        if not str(file_path).startswith(str(PUBLIC_DIR.resolve())) or not file_path.exists():
            json_response(self, {"error": "Not found"}, status=404)
            return

        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- 旧端点 ----

    def handle_list_feedbacks(self, query):
        params = parse_qs(query)
        limit = int(params.get("limit", ["100"])[0])
        status = params.get("status", [""])[0]
        groups = []
        for value in params.get("group", []):
            groups.extend([name.strip() for name in value.split(",") if name.strip()])

        sql = "SELECT * FROM feedbacks"
        values = []
        clauses = []
        if status:
            clauses.append("status = ?")
            values.append(status)
        if groups:
            placeholders = ",".join("?" for _ in groups)
            clauses.append(f"source_group IN ({placeholders})")
            values.extend(groups)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        values.append(min(max(limit, 1), 500))

        with get_db() as conn:
            rows = conn.execute(sql, values).fetchall()
        json_response(self, {"items": rows_to_dicts(rows)})

    def handle_run_summary(self):
        payload = read_json(self)
        send_to_wechat = bool(payload.get("send_to_wechat"))
        result = create_summary(
            send_to_wechat=send_to_wechat,
            mode=(payload.get("mode") or "pending"),
            limit=int(payload.get("limit") or 100),
            group_names=payload.get("groups") or [],
        )
        json_response(self, result, status=200 if result.get("ok") else 400)

    def handle_list_summaries(self, query):
        params = parse_qs(query)
        start_date = (params.get("start", [""])[0] or "").strip()
        end_date = (params.get("end", [""])[0] or "").strip()
        source = (params.get("source", [""])[0] or "").strip()

        values = []
        clauses = []
        if source == "community":
            clauses.append("content LIKE ?")
            values.append("# Muse AI 社群 AI 总结%")
        if start_date:
            clauses.append("created_at >= ?")
            values.append(f"{start_date} 00:00:00")
        if end_date:
            clauses.append("created_at <= ?")
            values.append(f"{end_date} 23:59:59")

        sql = "SELECT * FROM summaries"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT 50"

        with get_db() as conn:
            rows = conn.execute(sql, values).fetchall()
        json_response(self, {"items": rows_to_dicts(rows)})

    def handle_list_groups(self):
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, display_name, kind, enabled, last_message_time, last_message_id, updated_at
                FROM monitored_chats
                ORDER BY enabled DESC, display_name ASC
                """
            ).fetchall()
        items = rows_to_dicts(rows)
        for item in items:
            item["enabled"] = bool(item["enabled"])
        json_response(self, {"items": items})

    def handle_sync_groups(self):
        run_json_command(wecom_command("decrypt", *wecom_data_args()), timeout=180)
        data = run_json_command(wecom_command("sessions", "--limit", "500"), timeout=120)
        sessions = data.get("sessions") or []
        group_sessions = [item for item in sessions if item.get("kind") == "群聊"]
        updated_at = now_iso()
        default_chat_name = env_value("WECOM_CHAT_NAME", "测试群")

        with get_db() as conn:
            for item in group_sessions:
                display_name = item.get("display_name") or item.get("conversation_id")
                conn.execute(
                    """
                    INSERT INTO monitored_chats
                    (chat_id, display_name, kind, enabled, last_message_time, last_message_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        kind = excluded.kind,
                        last_message_time = excluded.last_message_time,
                        last_message_id = excluded.last_message_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        item.get("conversation_id") or display_name,
                        display_name,
                        item.get("kind") or "",
                        1 if display_name == default_chat_name else 0,
                        int(item.get("last_message_time") or 0),
                        int(item.get("last_message_id") or 0),
                        updated_at,
                    ),
                )

        json_response(self, {"ok": True, "count": len(group_sessions)})

    def handle_update_groups(self):
        payload = read_json(self)
        enabled = payload.get("enabled") or []
        if not isinstance(enabled, list):
            json_response(self, {"error": "enabled 必须是数组"}, status=400)
            return

        enabled_set = {str(item) for item in enabled}
        with get_db() as conn:
            rows = conn.execute("SELECT chat_id FROM monitored_chats").fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE monitored_chats SET enabled = ? WHERE chat_id = ?",
                    (1 if row["chat_id"] in enabled_set else 0, row["chat_id"]),
                )

        json_response(self, {"ok": True, "enabled_count": len(enabled_set)})

    def handle_import_once(self):
        spawn_import_once()
        json_response(self, {"ok": True, "message": "已开始采集所选群"})

    def handle_get_settings(self):
        json_response(
            self,
            {
                "auto_interval_minutes": get_auto_interval_minutes(),
                "last_auto_import_at": get_setting("last_auto_import_at") or "",
            },
        )

    def handle_update_settings(self):
        payload = read_json(self)
        try:
            minutes = int(payload.get("auto_interval_minutes"))
        except (TypeError, ValueError):
            json_response(self, {"error": "auto_interval_minutes 必须是整数"}, status=400)
            return
        if minutes < 0:
            json_response(self, {"error": "间隔不能为负数"}, status=400)
            return
        set_setting("auto_interval_minutes", minutes)
        json_response(self, {"ok": True, "auto_interval_minutes": minutes})

    def handle_close_feedback(self):
        payload = read_json(self)
        ids = payload.get("ids") or []
        if not isinstance(ids, list) or not ids:
            json_response(self, {"error": "请选择要关闭的反馈"}, status=400)
            return

        with get_db() as conn:
            conn.executemany(
                "UPDATE feedbacks SET status = 'closed' WHERE id = ?",
                [(int(item_id),) for item_id in ids],
            )
        json_response(self, {"ok": True, "count": len(ids)})

    # ---- 新端点 ----

    def handle_list_messages(self, query):
        params = parse_qs(query)
        limit = min(max(int(params.get("limit", ["50"])[0]), 1), 200)
        offset = max(int(params.get("offset", ["0"])[0]), 0)

        clauses, values = [], []
        for key, column in (("group", "source_group"), ("noise", "is_noise"), ("classified", "classified")):
            value = params.get(key, [""])[0]
            if value != "":
                clauses.append(f"{column} = ?")
                values.append(value)
        start_date = (params.get("start", [""])[0] or "").strip()
        if start_date:
            clauses.append("created_at >= ?")
            values.append(f"{start_date} 00:00:00")
        end_date = (params.get("end", [""])[0] or "").strip()
        if end_date:
            clauses.append("created_at <= ?")
            values.append(f"{end_date} 23:59:59")

        sql = "SELECT * FROM messages"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        values.extend([limit, offset])

        with get_db() as conn:
            rows = conn.execute(sql, values).fetchall()
        json_response(self, {"items": rows_to_dicts(rows)})

    def handle_run_classify(self):
        payload = read_json(self)
        result = run_classify_async(payload)
        json_response(self, result, status=200 if result.get("ok") else 400)

    def handle_classify_status(self):
        running = get_setting("classify_running") == "1"
        last_raw = get_setting("classify_last_result")
        last = None
        if last_raw:
            try:
                last = json.loads(last_raw)
            except json.JSONDecodeError:
                last = None
        json_response(self, {"running": running, "last": last})

    def handle_list_events(self, query):
        params = parse_qs(query)
        limit = min(max(int(params.get("limit", ["50"])[0]), 1), 200)
        offset = max(int(params.get("offset", ["0"])[0]), 0)

        clauses, values = [], []
        for key, column in (
            ("type", "event_type"),
            ("status", "status"),
            ("risk_level", "risk_level"),
            ("group", "source_group"),
        ):
            value = params.get(key, [""])[0]
            if value != "":
                clauses.append(f"e.{column} = ?")
                values.append(value)
        start_date = (params.get("start", [""])[0] or "").strip()
        if start_date:
            clauses.append("e.created_at >= ?")
            values.append(f"{start_date} 00:00:00")
        end_date = (params.get("end", [""])[0] or "").strip()
        if end_date:
            clauses.append("e.created_at <= ?")
            values.append(f"{end_date} 23:59:59")

        sql = """
            SELECT e.*, m.content, m.sender_name, m.sender_id
            FROM events e JOIN messages m ON m.id = e.message_id
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY e.created_at DESC, e.id DESC LIMIT ? OFFSET ?"
        values.extend([limit, offset])

        with get_db() as conn:
            rows = conn.execute(sql, values).fetchall()
        json_response(self, {"items": rows_to_dicts(rows)})

    def handle_confirm_events(self):
        payload = read_json(self)
        ids = payload.get("ids") or []
        action = payload.get("action")
        note = payload.get("note") or ""
        if action not in ("confirm", "reject"):
            json_response(self, {"error": "action 必须是 confirm 或 reject"}, status=400)
            return
        if not isinstance(ids, list) or not ids:
            json_response(self, {"error": "请选择事件"}, status=400)
            return

        status = "confirmed" if action == "confirm" else "rejected"
        with get_db() as conn:
            conn.executemany(
                "UPDATE events SET status = ?, status_note = ?, status_at = ? WHERE id = ?",
                [(status, note, now_iso(), int(item_id)) for item_id in ids],
            )
        json_response(self, {"ok": True, "count": len(ids), "status": status})

    def handle_repush_events(self):
        payload = read_json(self)
        ids = payload.get("ids") or []
        if not isinstance(ids, list) or not ids:
            json_response(self, {"error": "请选择事件"}, status=400)
            return

        pushed, failed = [], []
        for item_id in ids:
            ok, error = push_risk_event(int(item_id))
            if ok:
                pushed.append(int(item_id))
            else:
                failed.append({"id": int(item_id), "error": error})
        json_response(self, {"ok": not failed, "pushed": pushed, "failed": failed})

    def handle_list_demands(self):
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.name, d.direction, d.first_seen, d.is_interesting,
                       d.status, d.note,
                       COUNT(e.id) AS cnt,
                       COALESCE(SUM(CASE WHEN e.created_at >= ? THEN 1 ELSE 0 END), 0) AS cnt_7d,
                       COALESCE(SUM(CASE WHEN e.created_at >= ? THEN 1 ELSE 0 END), 0) AS cnt_30d
                FROM demands d
                LEFT JOIN events e
                    ON e.demand_id = d.id AND e.event_type = 'demand'
                    AND e.status IN ('pending','confirmed')
                GROUP BY d.id
                ORDER BY d.status ASC, cnt DESC, d.first_seen DESC
                LIMIT 300
                """,
                (
                    (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
                    (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            ).fetchall()
        json_response(self, {"items": rows_to_dicts(rows)})

    def handle_update_demand(self):
        payload = read_json(self)
        try:
            demand_id = int(payload.get("id"))
        except (TypeError, ValueError):
            json_response(self, {"error": "id 必须是整数"}, status=400)
            return

        name = (payload.get("name") or "").strip()
        direction = (payload.get("direction") or "").strip()
        is_interesting = payload.get("is_interesting")
        merge_into = payload.get("merge_into")

        with get_db() as conn:
            demand = conn.execute(
                "SELECT * FROM demands WHERE id = ?", (demand_id,)
            ).fetchone()
            if demand is None:
                json_response(self, {"error": "需求不存在"}, status=404)
                return

            # 改名时若目标名已存在 → 等价于合并到已有需求
            if name and name != demand["name"]:
                existing = conn.execute(
                    "SELECT id FROM demands WHERE name = ? AND id != ?", (name, demand_id)
                ).fetchone()
                if existing:
                    merge_into = existing["id"]

            if merge_into:
                target = conn.execute(
                    "SELECT * FROM demands WHERE id = ?", (int(merge_into),)
                ).fetchone()
                if target is None or target["id"] == demand_id:
                    json_response(self, {"error": "合并目标不存在或为自身"}, status=400)
                    return
                conn.execute(
                    "UPDATE events SET demand_id = ? WHERE demand_id = ?",
                    (target["id"], demand_id),
                )
                conn.execute(
                    """
                    UPDATE demands SET first_seen = MIN(first_seen, ?) WHERE id = ?
                    """,
                    (demand["first_seen"], target["id"]),
                )
                if demand["direction"] and not target["direction"]:
                    conn.execute(
                        "UPDATE demands SET direction = ? WHERE id = ?",
                        (demand["direction"], target["id"]),
                    )
                if demand["is_interesting"]:
                    conn.execute(
                        "UPDATE demands SET is_interesting = 1 WHERE id = ?", (target["id"],)
                    )
                conn.execute("UPDATE demands SET status = 'merged' WHERE id = ?", (demand_id,))
            else:
                if name:
                    conn.execute("UPDATE demands SET name = ? WHERE id = ?", (name, demand_id))
                if direction:
                    conn.execute(
                        "UPDATE demands SET direction = ? WHERE id = ?", (direction, demand_id)
                    )
                if is_interesting is not None:
                    conn.execute(
                        "UPDATE demands SET is_interesting = ? WHERE id = ?",
                        (1 if is_interesting else 0, demand_id),
                    )

        json_response(self, {"ok": True, "merged_into": int(merge_into) if merge_into else None})

    def handle_list_reports(self, query):
        params = parse_qs(query)
        report_type = params.get("type", [""])[0] or None
        start_date = (params.get("start", [""])[0] or "").strip()
        end_date = (params.get("end", [""])[0] or "").strip()
        items = list_reports(report_type=report_type, start_date=start_date, end_date=end_date)
        json_response(self, {"items": items})

    def handle_run_report(self):
        payload = read_json(self)
        report_type = payload.get("type") or "daily"
        if report_type not in ("daily", "weekly"):
            json_response(self, {"error": "type 必须是 daily 或 weekly"}, status=400)
            return
        result = generate_report_core(
            report_type=report_type,
            date_str=payload.get("date"),
            force=bool(payload.get("force")),
            push=bool(payload.get("push")),
        )
        json_response(self, result, status=200 if result.get("ok") else 400)

    def handle_panel_stats(self, query):
        params = parse_qs(query)
        try:
            days = int(params.get("days", ["7"])[0])
        except (TypeError, ValueError):
            days = 7
        json_response(self, panel_stats(days=days))

    # ---- AI 问答 ----

    def sse_begin(self):
        # 默认 HTTP/1.0，流式必须 1.1（BaseHTTPRequestHandler 每请求一个实例，无跨请求影响）
        self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.close_connection = True  # 流结束后立即关闭，客户端以 EOF 感知结束
        self.end_headers()  # 不设 Content-Length → 分块传输

    def sse_send(self, data, event=None):
        # data 为 dict，整体 JSON 编码成单行帧，增量里的换行不破坏 SSE 帧边界
        lines = []
        if event:
            lines.append(f"event: {event}")
        lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
        try:
            self.wfile.write(("\n".join(lines) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False  # 客户端断开 → 调用方停止生成

    def handle_chat_history(self):
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, role, content, created_at FROM chat_messages ORDER BY id ASC LIMIT 500"
            ).fetchall()
        json_response(self, {"items": rows_to_dicts(rows)})

    def handle_chat_clear(self):
        with get_db() as conn:
            cursor = conn.execute("DELETE FROM chat_messages")
        json_response(self, {"ok": True, "cleared": cursor.rowcount})

    def handle_chat_stream(self):
        payload = read_json(self)
        question = str(payload.get("message") or "").strip()
        if not question:
            json_response(self, {"error": "消息内容不能为空"}, status=400)
            return
        if len(question) > MAX_QUESTION_CHARS:
            json_response(self, {"error": f"消息过长（最多 {MAX_QUESTION_CHARS} 字）"}, status=400)
            return

        # 1) 先持久化用户消息（生成失败也不丢），并取最近历史
        with get_db() as conn:
            conn.execute(
                "INSERT INTO chat_messages (role, content, created_at) VALUES ('user', ?, ?)",
                (question, now_iso()),
            )
            history_rows = conn.execute(
                "SELECT role, content FROM chat_messages ORDER BY id DESC LIMIT ?",
                (HISTORY_TURNS * 2,),
            ).fetchall()
        history_rows.reverse()

        # 2) 检索证据（本地 SQL，毫秒级）
        try:
            with get_db() as conn:
                evidence = retrieve_evidence(question, conn)
        except Exception as exc:
            traceback.print_exc()
            json_response(self, {"error": f"检索证据失败：{exc}"}, status=500)
            return

        # 3) 无证据 → 不调 LLM，直接流式返回固定话术
        if not evidence["items"]:
            canned = canned_no_evidence()
            self.sse_begin()
            self.sse_send({"delta": canned})
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO chat_messages (role, content, created_at) VALUES ('assistant', ?, ?)",
                    (canned, now_iso()),
                )
            self.sse_send({"delta": ""}, event="done")
            return

        # 4) 组装消息，流式调用
        messages = build_llm_messages(
            history_rows, question, serialize_evidence(evidence["items"])
        )
        self.sse_begin()
        full_text, emitted = "", 0
        attempt = 0
        while attempt < 2:
            try:
                for delta in call_model_stream(messages, max_tokens=2000):
                    emitted += 1
                    full_text += delta
                    if not self.sse_send({"delta": delta}):
                        return  # 客户端断开，不落库半截回答
                break
            except RetriableModelError as exc:
                attempt += 1
                if attempt >= 2 or emitted > 0:
                    self.sse_send({"error": f"模型接口错误：{exc}"}, event="error")
                    return
            except Exception as exc:
                traceback.print_exc()
                self.sse_send({"error": f"回答失败：{exc}"}, event="error")
                return

        if full_text:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO chat_messages (role, content, created_at) VALUES ('assistant', ?, ?)",
                    (full_text, now_iso()),
                )
        self.sse_send({"delta": ""}, event="done")

    def log_message(self, format, *args):
        return


def main():
    load_env()
    init_db()

    scheduler = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler.start()

    report_scheduler = threading.Thread(target=report_scheduler_loop, daemon=True)
    report_scheduler.start()

    port = int(os.environ.get("PORT", "8787") or "8787")
    server = None
    for candidate_port in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate_port), AppHandler)
            port = candidate_port
            break
        except OSError as exc:
            if exc.errno != 48:
                raise

    if server is None:
        raise RuntimeError(f"端口 {port}-{port + 9} 都被占用了，请在 .env 里修改 PORT")

    RUNTIME_PATH.write_text(
        json.dumps({"port": port, "url": f"http://127.0.0.1:{port}"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"客服反馈系统已启动：http://127.0.0.1:{port}")
    print(f"分析面板：http://127.0.0.1:{port}/panel")
    print("按 Ctrl+C 停止服务")
    server.serve_forever()


if __name__ == "__main__":
    main()
