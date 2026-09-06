/* Muse AI 社群分析面板（原生 JS，无依赖） */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const EVENT_META = {
    risk: { label: "风险", cls: "risk" },
    feedback: { label: "反馈", cls: "feedback" },
    demand: { label: "需求", cls: "demand" },
    style: { label: "曲风", cls: "style" },
    release_issue: { label: "发行问题", cls: "release" },
    release_intel: { label: "发行资讯", cls: "release" },
    question: { label: "疑问", cls: "question" },
    sentiment: { label: "情绪", cls: "sentiment" },
  };
  const RISK_LABEL = { 1: "一级·提醒", 2: "二级·警告", 3: "三级·高风险" };
  const STATUS_LABEL = { pending: "待确认", confirmed: "已确认", rejected: "已驳回" };
  const PUSH_LABEL = { not_needed: "无需推送", pushed: "已推送", failed: "推送失败" };
  const NOISE_LABEL = { "": "正常", repeat_flood: "刷屏" };

  /* ---------- 小工具 ---------- */

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function toast(message) {
    const node = document.createElement("div");
    node.className = "toast";
    node.textContent = message;
    document.body.appendChild(node);
    setTimeout(() => node.classList.add("show"), 20);
    setTimeout(() => {
      node.classList.remove("show");
      setTimeout(() => node.remove(), 400);
    }, 2600);
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || response.statusText);
    return data;
  }

  const getJSON = (path) => api(path);
  const postJSON = (path, body) =>
    api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });

  function fmtDate(value) {
    if (!value) return "";
    const iso = String(value).replace(" ", "T");
    const date = new Date(iso);
    if (isNaN(date.getTime())) return esc(value);
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getMonth() + 1}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  /* ---------- 顶部统计条 ---------- */

  async function loadStats() {
    try {
      const stats = await getJSON("/api/panel/stats?days=7");
      $("statTodayMessages").textContent = stats.today_messages;
      $("statWeekMessages").textContent = stats.week_messages;
      $("statWeekUsers").textContent = stats.week_users;
      $("statPendingClassify").textContent = stats.pending_classify;
      $("statPendingConfirm").textContent = stats.pending_confirm;
      $("statActiveDemands").textContent = stats.active_demands;
      const risk = stats.risk_counts || {};
      $("statRisk1").textContent = risk["1"] || 0;
      $("statRisk2").textContent = risk["2"] || 0;
      $("statRisk3").textContent = risk["3"] || 0;
    } catch (error) {
      toast("统计加载失败：" + error.message);
    }
  }

  /* ---------- 事件列表渲染（风险/事件 tab 共用） ---------- */

  function eventLine(event) {
    const meta = EVENT_META[event.event_type] || { label: event.event_type, cls: "" };
    const typeTag = `<span class="tag event-tag ${meta.cls}">${meta.label}</span>`;
    const levelTag = event.risk_level
      ? `<span class="tag risk-tag risk-lv${event.risk_level}">${RISK_LABEL[event.risk_level] || event.risk_level}</span>`
      : "";
    const statusTag = `<span class="tag status-tag status-${event.status}">${STATUS_LABEL[event.status] || event.status}</span>`;
    const pushTag = event.event_type === "risk"
      ? `<span class="tag push-tag push-${event.push_status}">${PUSH_LABEL[event.push_status] || event.push_status}</span>`
      : "";

    let headline;
    if (event.event_type === "risk") {
      const category = event.payload ? safePayload(event.payload).category : "";
      headline = (category ? "【" + category + "】" : "") + (event.value_text || "风险事件");
    } else if (event.event_type === "feedback") {
      headline = "【" + (event.key_field || "功能") + "】" + (event.value_text || "");
    } else if (event.event_type === "demand") {
      headline = (event.key_field || "新需求") + (event.value_text ? "：" + event.value_text : "");
    } else if (event.event_type === "style") {
      headline = "想唱曲风【" + (event.key_field || "其他") + "】";
    } else if (event.event_type === "release_issue") {
      const parts = ["发行问题【" + (event.key_field || "其他") + "】"];
      if (event.key_field2) parts.push(event.key_field2);
      if (event.value_text) parts.push(event.value_text);
      headline = parts.join(" · ");
    } else if (event.event_type === "release_intel") {
      headline = "发行资讯" + (event.key_field2 ? "（" + event.key_field2 + "）" : "") + (event.value_text ? "：" + event.value_text : "");
    } else if (event.event_type === "question") {
      headline = "疑问【" + (event.key_field || "主题") + "】" + (event.value_text ? "：" + event.value_text : "");
    } else if (event.event_type === "sentiment") {
      const emoji = { pos: "😊", neu: "😐", neg: "😟" }[event.key_field] || "";
      headline = emoji + " 情绪：" + ({ pos: "正面", neu: "中性", neg: "负面" }[event.key_field] || event.key_field);
      if (event.value_text) headline += "（" + event.value_text + "）";
    } else {
      headline = event.value_text || event.key_field || "";
    }

    const sender = event.sender_name || event.sender_key || "unknown";
    const actions = [];
    if (event.status === "pending") {
      actions.push(`<button type="button" class="mini ok" data-action="confirm">✓ 确认</button>`);
      actions.push(`<button type="button" class="mini bad" data-action="reject">✗ 驳回</button>`);
    }
    if (event.event_type === "risk" && ["2", "3"].includes(String(event.risk_level))) {
      actions.push(`<button type="button" class="mini" data-action="repush">重推</button>`);
    }

    return `
      <article class="event-card ${event.status === "rejected" ? "dim" : ""}" data-id="${event.id}">
        <div class="event-head">
          ${typeTag}${levelTag}${statusTag}${pushTag}
          <span class="event-meta muted">${fmtDate(event.created_at)} · ${esc(event.source_group)} · ${esc(sender)}</span>
          <span class="flex-spacer"></span>
          <span class="event-actions">${actions.join("")}</span>
        </div>
        <p class="event-line">${headline}</p>
        ${event.content ? `<blockquote class="event-quote">${esc(event.content)}</blockquote>` : ""}
      </article>`;
  }

  function safePayload(raw) {
    if (!raw) return {};
    if (typeof raw === "object") return raw;
    try {
      return JSON.parse(raw);
    } catch (e) {
      return {};
    }
  }

  async function loadEvents(listId, params) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params || {})) {
      if (value) query.set(key, value);
    }
    query.set("limit", "100");
    try {
      const data = await getJSON("/api/events?" + query.toString());
      $(listId).innerHTML = (data.items || []).map(eventLine).join("") || emptyHint();
      bindEventActions(listId);
    } catch (error) {
      toast("事件加载失败：" + error.message);
    }
  }

  function emptyHint() {
    return '<p class="muted empty-hint">没有符合条件的事件。</p>';
  }

  function bindEventActions(listId) {
    $(listId).querySelectorAll(".event-card").forEach((card) => {
      card.querySelectorAll("button[data-action]").forEach((button) => {
        button.addEventListener("click", async () => {
          const id = parseInt(card.dataset.id, 10);
          const action = button.dataset.action;
          try {
            if (action === "confirm" || action === "reject") {
              await postJSON("/api/events/confirm", { ids: [id], action });
              toast(action === "confirm" ? "已确认" : "已驳回");
            } else if (action === "repush") {
              await postJSON("/api/events/repush", { ids: [id] });
              toast("已重新推送");
            }
            refreshCurrentTab();
            loadStats();
          } catch (error) {
            toast("操作失败：" + error.message);
          }
        });
      });
    });
  }

  /* ---------- 需求 tab ---------- */

  async function loadDemands() {
    try {
      const data = await getJSON("/api/demands");
      const items = data.items || [];
      $("demandList").innerHTML = items.map(demandCard).join("") || emptyHint();
      bindDemandActions();
    } catch (error) {
      toast("需求加载失败：" + error.message);
    }
  }

  function demandCard(demand) {
    const interesting = demand.is_interesting ? "⭐ " : "";
    const merged = demand.status === "merged" ? " dim" : "";
    return `
      <article class="event-card demand-card${merged}" data-id="${demand.id}" data-name="${esc(demand.name)}">
        <div class="event-head">
          <span class="tag event-tag demand">需求</span>
          ${demand.status === "merged" ? '<span class="tag status-tag status-rejected">已合并</span>' : ""}
          <span class="event-meta muted">首次出现 ${fmtDate(demand.first_seen)}</span>
          <span class="flex-spacer"></span>
          <span class="demand-actions">
            <button type="button" class="mini" data-action="interesting">${demand.is_interesting ? "取消有趣" : "标有趣"}</button>
            <button type="button" class="mini" data-action="rename">改名/合并</button>
          </span>
        </div>
        <p class="event-line">${interesting}${esc(demand.name)}</p>
        ${demand.direction ? `<p class="demand-direction muted">方向：${esc(demand.direction)}</p>` : ""}
        <p class="demand-counts muted">人数 ${demand.cnt || 0} · 近 7 天 ${demand.cnt_7d || 0} · 近 30 天 ${demand.cnt_30d || 0}</p>
      </article>`;
  }

  function bindDemandActions() {
    $("demandList").querySelectorAll(".demand-card").forEach((card) => {
      card.querySelectorAll("button[data-action]").forEach((button) => {
        button.addEventListener("click", async () => {
          const id = parseInt(card.dataset.id, 10);
          try {
            if (button.dataset.action === "interesting") {
              const current = button.textContent.includes("取消");
              await postJSON("/api/demands/update", { id, is_interesting: !current });
              toast("已更新");
            } else if (button.dataset.action === "rename") {
              const name = prompt("新的需求名称（与已有需求同名将直接合并）：", card.dataset.name);
              if (!name || !name.trim()) return;
              const result = await postJSON("/api/demands/update", { id, name: name.trim() });
              toast(result.merged_into ? "已合并到同名需求" : "已改名");
            }
            loadDemands();
            loadStats();
          } catch (error) {
            toast("操作失败：" + error.message);
          }
        });
      });
    });
  }

  /* ---------- 报表 tab ---------- */

  async function loadReports() {
    const type = $("reportTypeSelect").value;
    try {
      const data = await getJSON("/api/reports?type=" + type);
      const items = data.items || [];
      $("reportList").innerHTML = items.map(reportCard).join("") || emptyHint();
    } catch (error) {
      toast("报表加载失败：" + error.message);
    }
  }

  function reportCard(report) {
    const title = report.report_type === "daily" ? "日报" : "周报";
    const pushTag = report.pushed_to_wechat
      ? '<span class="tag push-tag push-pushed">已推群</span>'
      : "";
    return `
      <article class="event-card report-card">
        <div class="event-head">
          <span class="tag event-tag report">${title}</span>
          ${pushTag}
          <span class="event-meta muted">${esc(report.period_start)} ~ ${esc(report.period_end)} · 生成于 ${fmtDate(report.generated_at)}</span>
        </div>
        <div class="report-content markdown-body">${renderMarkdown(report.content)}</div>
      </article>`;
  }

  async function runReport() {
    const button = $("runReport");
    button.disabled = true;
    button.textContent = "生成中…";
    try {
      const payload = {
        type: $("reportTypeSelect").value,
        force: $("reportForce").checked,
        push: $("reportPush").checked,
      };
      if ($("reportDate").value) payload.date = $("reportDate").value;
      await postJSON("/api/reports/run", payload);
      toast("报表已生成");
      await loadReports();
    } catch (error) {
      toast("生成失败：" + error.message);
    } finally {
      button.disabled = false;
      button.textContent = "立即生成";
    }
  }

  /* ---------- 消息流 tab ---------- */

  async function loadMessages() {
    const filter = $("messageFilter").value;
    const query = filter ? new URLSearchParams({ [filter]: "1", limit: "100" }) : new URLSearchParams({ limit: "100" });
    try {
      const data = await getJSON("/api/messages?" + query.toString());
      $("messageList").innerHTML = (data.items || []).map(messageCard).join("") || emptyHint();
    } catch (error) {
      toast("消息加载失败：" + error.message);
    }
  }

  function messageCard(message) {
    const noiseTag = message.is_noise
      ? `<span class="tag push-tag push-failed">噪音·${NOISE_LABEL[message.noise_rule] || message.noise_rule}</span>`
      : "";
    const classTag = message.classified === 1
      ? '<span class="tag push-tag push-pushed">已分类</span>'
      : message.classified === 2
        ? '<span class="tag push-tag push-failed">失败</span>'
        : '<span class="tag push-tag">未分类</span>';
    const sender = message.sender_name || message.sender_key || "unknown";
    return `
      <article class="event-card message-card">
        <div class="event-head">
          ${noiseTag}${classTag}
          <span class="event-meta muted">${fmtDate(message.created_at)} · ${esc(message.source_group)} · ${esc(sender)}</span>
        </div>
        <p class="event-line message-content">${esc(message.content)}</p>
      </article>`;
  }

  async function runClassify() {
    const button = $("runClassify");
    button.disabled = true;
    try {
      const result = await postJSON("/api/classify/run", {
        retry_failed: $("retryFailed").checked,
      });
      if (!result.ok) {
        toast(result.message || "分类任务未启动");
        return;
      }
      toast("分类任务已启动");
      pollClassify(0);
    } catch (error) {
      toast("启动失败：" + error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function pollClassify(tick) {
    try {
      const status = await getJSON("/api/classify/status");
      if (status.running) {
        const last = status.last || {};
        const dots = ".".repeat((tick % 3) + 1);
        $("classifyStatus").textContent =
          "分类进行中" + dots +
          (last.classified ? "（已分类 " + last.classified + " 条）" : "");
        setTimeout(() => pollClassify(tick + 1), 3000);
      } else {
        const last = status.last || {};
        $("classifyStatus").textContent = last.error
          ? "分类出错：" + last.error
          : "上次分类：" + (last.finished_at ? fmtDate(last.finished_at) : "–") +
            (last.classified != null ? "，处理 " + last.classified + " 条" : "");
        loadMessages();
        loadStats();
      }
    } catch (error) {
      $("classifyStatus").textContent = "状态查询失败";
    }
  }

  /* ---------- tab 切换 ---------- */

  const TAB_LOADERS = {
    risk: () => loadEvents("riskList", { type: "risk", risk_level: $("riskLevelFilter").value, status: $("riskStatusFilter").value }),
    events: () => loadEvents("eventList", { type: $("eventTypeFilter").value, status: $("eventStatusFilter").value }),
    demands: loadDemands,
    reports: loadReports,
    messages: () => { loadMessages(); loadClassifyStatus(); },
  };

  let currentTab = "risk";

  function switchTab(tab) {
    currentTab = tab;
    $("panelTabs").querySelectorAll("button").forEach((button) => {
      button.classList.toggle("active", button.dataset.tab === tab);
    });
    document.querySelectorAll(".tab-page").forEach((page) => {
      page.classList.toggle("active", page.id === "tab-" + tab);
    });
    TAB_LOADERS[tab]();
  }

  function refreshCurrentTab() {
    TAB_LOADERS[currentTab]();
  }

  async function loadClassifyStatus() {
    try {
      const status = await getJSON("/api/classify/status");
      if (status.running) {
        pollClassify(0);
      } else {
        const last = status.last || {};
        $("classifyStatus").textContent = last.error
          ? "分类出错：" + last.error
          : "上次分类：" + (last.finished_at ? fmtDate(last.finished_at) : "–") +
            (last.classified != null ? "，处理 " + last.classified + " 条" : "");
      }
    } catch (e) { /* 忽略 */ }
  }

  /* ---------- 初始化 ---------- */

  function init() {
    $("panelTabs").querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => switchTab(button.dataset.tab));
    });

    $("riskRefresh").addEventListener("click", () => TAB_LOADERS.risk());
    $("riskLevelFilter").addEventListener("change", () => TAB_LOADERS.risk());
    $("riskStatusFilter").addEventListener("change", () => TAB_LOADERS.risk());
    $("eventsRefresh").addEventListener("click", () => TAB_LOADERS.events());
    $("eventTypeFilter").addEventListener("change", () => TAB_LOADERS.events());
    $("eventStatusFilter").addEventListener("change", () => TAB_LOADERS.events());
    $("demandsRefresh").addEventListener("click", loadDemands);
    $("reportsRefresh").addEventListener("click", loadReports);
    $("reportTypeSelect").addEventListener("change", loadReports);
    $("runReport").addEventListener("click", runReport);
    $("messagesRefresh").addEventListener("click", loadMessages);
    $("messageFilter").addEventListener("change", loadMessages);
    $("runClassify").addEventListener("click", runClassify);

    loadStats();
    switchTab("risk");
    setInterval(loadStats, 30000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
