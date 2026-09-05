/* Muse AI 社群客服与用户洞察首页 */

const refreshButton = document.querySelector("#refreshList");
const runSummaryButton = document.querySelector("#runSummary");
const rerunSummaryButton = document.querySelector("#rerunSummary");
const summaryLimit = document.querySelector("#summaryLimit");
const sendWechat = document.querySelector("#sendWechat");
const pendingCount = document.querySelector("#pendingCount");
const summaryOutput = document.querySelector("#summaryOutput");
const summaryHistory = document.querySelector("#summaryHistory");
const summaryDateStart = document.querySelector("#summaryDateStart");
const summaryDateEnd = document.querySelector("#summaryDateEnd");
const clearDateFilterButton = document.querySelector("#clearDateFilter");
const autoInterval = document.querySelector("#autoInterval");
const autoStatus = document.querySelector("#autoStatus");
const importNowButton = document.querySelector("#importNow");
const syncGroupsButton = document.querySelector("#syncGroups");
const saveGroupsButton = document.querySelector("#saveGroups");
const groupList = document.querySelector("#groupList");
const groupStatus = document.querySelector("#groupStatus");
const chatForm = document.querySelector("#chatForm");
const chatInput = document.querySelector("#chatInput");
const chatSend = document.querySelector("#chatSend");
const chatMessages = document.querySelector("#chatMessages");
const clearChatButton = document.querySelector("#clearChat");
let chatStreaming = false;

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || data.message || "请求失败");
  }
  return data;
}

function showToast(message) {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2200);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatInterval(minutes) {
  if (!minutes) return "已关闭";
  if (minutes === 30) return "每半小时自动采集并分类";
  if (minutes === 60) return "每 1 小时自动采集并分类";
  if (minutes === 120) return "每 2 小时自动采集并分类";
  if (minutes === 1440) return "每天自动采集并分类";
  return `每 ${minutes} 分钟自动采集并分类`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function refreshHomeStats() {
  await Promise.all([loadFeedbacks(), loadStats()]);
}

async function loadFeedbacks() {
  const data = await requestJson("/api/feedbacks?status=open&limit=80");
  const items = data.items || [];
  const waiting = items.filter((item) => item.summary_id === null).length;
  pendingCount.textContent = `待总结 ${waiting} 条`;
}

async function loadStats() {
  const stats = await requestJson("/api/panel/stats?days=7");
  document.querySelector("#statTodayMessages").textContent = stats.today_messages;
  document.querySelector("#statWeekMessages").textContent = stats.week_messages;
  document.querySelector("#statWeekUsers").textContent = stats.week_users;
  document.querySelector("#statPendingConfirm").textContent = stats.pending_confirm;
}

async function loadSettings() {
  const settings = await requestJson("/api/settings");
  const minutes = Number(settings.auto_interval_minutes || 0);
  autoInterval.value = String(minutes);
  autoStatus.textContent = formatInterval(minutes);
}

async function saveInterval() {
  const minutes = Number(autoInterval.value || 0);
  await requestJson("/api/settings", {
    method: "POST",
    body: JSON.stringify({ auto_interval_minutes: minutes }),
  });
  autoStatus.textContent = formatInterval(minutes);
  showToast("自动采集间隔已更新");
}

function selectedGroupNames() {
  return Array.from(groupList.querySelectorAll("input:checked")).map((input) => input.dataset.name);
}

function updateGroupStatus() {
  const total = groupList.querySelectorAll("input").length;
  const selected = groupList.querySelectorAll("input:checked").length;
  groupStatus.textContent = total ? `已选择 ${selected} / ${total} 个群` : "暂无群聊，请先同步。";
}

async function loadGroups() {
  const data = await requestJson("/api/groups");
  const items = data.items || [];

  if (!items.length) {
    groupList.innerHTML = '<p class="muted">暂无群聊。</p>';
    updateGroupStatus();
    return;
  }

  groupList.innerHTML = items
    .map(
      (group) => `
        <label class="group-chip ${group.enabled ? "on" : ""}">
          <input type="checkbox" value="${escapeHtml(group.chat_id)}" data-name="${escapeHtml(
            group.display_name || group.chat_id,
          )}" ${group.enabled ? "checked" : ""} />
          <span>${escapeHtml(group.display_name || group.chat_id)}</span>
        </label>
      `,
    )
    .join("");

  groupList.querySelectorAll(".group-chip input").forEach((input) => {
    input.addEventListener("change", () => {
      input.closest(".group-chip").classList.toggle("on", input.checked);
      updateGroupStatus();
    });
  });
  updateGroupStatus();
}

async function saveGroups(silent = false) {
  const enabled = Array.from(groupList.querySelectorAll("input:checked")).map((input) => input.value);
  await requestJson("/api/groups/update", {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
  updateGroupStatus();
  if (!silent) {
    showToast(`已保存 ${enabled.length} 个群`);
  }
}

async function syncGroups() {
  syncGroupsButton.disabled = true;
  syncGroupsButton.textContent = "同步中";
  try {
    await requestJson("/api/groups/sync", { method: "POST", body: JSON.stringify({}) });
    await loadGroups();
    showToast("群列表已同步");
  } finally {
    syncGroupsButton.disabled = false;
    syncGroupsButton.textContent = "同步";
  }
}

async function importNow() {
  const originalText = importNowButton.textContent;
  importNowButton.disabled = true;
  importNowButton.textContent = "触发中";
  try {
    await saveGroups(true);
    await requestJson("/api/wecom/import-once", { method: "POST", body: JSON.stringify({}) });
    importNowButton.textContent = "采集中";
    showToast("已开始采集所选群，统计会自动刷新");
    for (let i = 0; i < 18; i += 1) {
      await sleep(5000);
      await refreshHomeStats();
    }
  } finally {
    importNowButton.textContent = originalText;
    importNowButton.disabled = false;
  }
}

function summaryDateQuery() {
  const params = new URLSearchParams({ source: "community" });
  if (summaryDateStart.value) params.append("start", summaryDateStart.value);
  if (summaryDateEnd.value) params.append("end", summaryDateEnd.value);
  const query = params.toString();
  return query ? `?${query}` : "";
}

async function loadSummaries() {
  const data = await requestJson(`/api/summaries${summaryDateQuery()}`);
  const items = data.items || [];

  if (!items.length) {
    const filtering = summaryDateStart.value || summaryDateEnd.value;
    summaryHistory.innerHTML = `<p class="muted">${
      filtering ? "该日期范围内暂无总结。" : "暂无新的社群 AI 总结，点击左侧立即总结生成。"
    }</p>`;
    return;
  }

  summaryHistory.innerHTML = items
    .map(
      (item, index) => `
        <article class="history-card ${index === 0 ? "expanded" : ""}">
          <time>${escapeHtml(item.created_at)} · ${item.feedback_count} 条消息</time>
          <div class="markdown-body">${renderMarkdown(item.content)}</div>
          <button type="button" class="toggle-summary">${index === 0 ? "收起" : "展开全文"}</button>
        </article>
      `,
    )
    .join("");

  summaryHistory.querySelectorAll(".toggle-summary").forEach((button) => {
    button.addEventListener("click", () => {
      const card = button.closest(".history-card");
      const expanded = card.classList.toggle("expanded");
      button.textContent = expanded ? "收起" : "展开全文";
    });
  });
}

async function loadWeekly() {
  const params = new URLSearchParams({ type: "weekly" });
  const start = document.querySelector("#weeklyDateStart").value;
  const end = document.querySelector("#weeklyDateEnd").value;
  if (start) params.append("start", start);
  if (end) params.append("end", end);
  const data = await requestJson(`/api/reports?${params.toString()}`);
  const items = data.items || [];
  const list = document.querySelector("#weeklyList");

  if (!items.length) {
    const filtering = start || end;
    list.innerHTML = `<p class="muted">${
      filtering ? "该日期范围暂无周报。" : "还没有生成周报。可在深度分析「报表」页或定时任务生成。"
    }</p>`;
    return;
  }

  list.innerHTML = items
    .map(
      (item, index) => `
        <article class="history-card ${index === 0 ? "expanded" : ""}">
          <time>${escapeHtml(item.period_start.slice(0, 10))} ~ ${escapeHtml(item.period_end.slice(0, 10))}${
            item.pushed_to_wechat ? " · 已推送企业微信" : ""
          } · 生成于 ${escapeHtml(item.generated_at)}</time>
          <div class="markdown-body">${renderMarkdown(item.content)}</div>
          <button type="button" class="toggle-summary">${index === 0 ? "收起" : "展开全文"}</button>
        </article>
      `,
    )
    .join("");

  list.querySelectorAll(".toggle-summary").forEach((button) => {
    button.addEventListener("click", () => {
      const card = button.closest(".history-card");
      const expanded = card.classList.toggle("expanded");
      button.textContent = expanded ? "收起" : "展开全文";
    });
  });
}

refreshButton.addEventListener("click", () => {
  refreshHomeStats().catch((error) => showToast(error.message));
  loadSummaries().catch((error) => showToast(error.message));
  loadWeekly().catch((error) => showToast(error.message));
});

/* ---------------- AI 问答 ---------------- */

function chatEmptyHtml() {
  return `<div class="chat-empty muted">向 AI 提问，例如：边骑车边写歌、骑车直播放歌 这是从哪个群哪个人得到的消息，给我群编号和原消息。</div>`;
}

function appendChatBubble(role, html, time) {
  const bubble = document.createElement("div");
  bubble.className = `chat-bubble ${role}`;
  if (time) {
    bubble.innerHTML = `<div class="chat-meta">${escapeHtml(time)}</div>`;
  }
  const body = document.createElement("div");
  body.className = "markdown-body";
  body.innerHTML = html;
  bubble.appendChild(body);
  chatMessages.appendChild(bubble);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return bubble;
}

async function loadChatHistory() {
  const data = await requestJson("/api/chat/history");
  const items = data.items || [];
  if (!items.length) {
    chatMessages.innerHTML = chatEmptyHtml();
    return;
  }
  chatMessages.innerHTML = "";
  for (const item of items) {
    const isUser = item.role === "user";
    appendChatBubble(isUser ? "user" : "assistant", isUser ? escapeHtml(item.content) : renderMarkdown(item.content), item.created_at);
  }
}

async function sendChat() {
  const text = chatInput.value.trim();
  if (!text || chatStreaming) return;
  chatStreaming = true;
  chatSend.disabled = true;
  chatInput.value = "";
  chatMessages.querySelector(".chat-empty")?.remove();
  appendChatBubble("user", escapeHtml(text), null);

  const bubble = appendChatBubble("assistant", "", null);
  bubble.classList.add("streaming");
  let buffer = "";
  const render = () => {
    bubble.querySelector(".markdown-body").innerHTML =
      renderMarkdown(buffer) + '<span class="chat-cursor">▍</span>';
    chatMessages.scrollTop = chatMessages.scrollHeight;
  };

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `请求失败（${response.status}）`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let raw = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      raw += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = raw.indexOf("\n\n")) !== -1) {
        const frame = raw.slice(0, sep);
        raw = raw.slice(sep + 2);
        let event = "message";
        const dataLines = [];
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
        }
        const payload = JSON.parse(dataLines.join("\n"));
        if (event === "error") throw new Error(payload.error || "回答失败");
        if (event === "done") {
          await reader.cancel().catch(() => {});
          break;
        }
        buffer += payload.delta || "";
        render();
      }
    }
    bubble.classList.remove("streaming");
    bubble.querySelector(".markdown-body").innerHTML = renderMarkdown(buffer);
  } catch (error) {
    bubble.classList.remove("streaming");
    bubble.querySelector(".markdown-body").innerHTML =
      renderMarkdown(buffer) + `<p class="chat-error">（回答中断：${escapeHtml(error.message)}）</p>`;
    showToast(error.message);
  } finally {
    chatStreaming = false;
    chatSend.disabled = false;
    chatInput.focus();
  }
}

chatForm.addEventListener("submit", (event) => {
  event.preventDefault();
  sendChat();
});
chatInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendChat();
  }
});
clearChatButton.addEventListener("click", async () => {
  if (chatStreaming) return;
  try {
    await requestJson("/api/chat/clear", { method: "POST", body: JSON.stringify({}) });
    chatMessages.innerHTML = chatEmptyHtml();
    showToast("对话已清空");
  } catch (error) {
    showToast(error.message);
  }
});

document.querySelector("#refreshWeekly").addEventListener("click", () => {
  loadWeekly().catch((error) => showToast(error.message));
});

document.querySelector("#weeklyDateStart").addEventListener("change", () =>
  loadWeekly().catch((error) => showToast(error.message)),
);
document.querySelector("#weeklyDateEnd").addEventListener("change", () =>
  loadWeekly().catch((error) => showToast(error.message)),
);
document.querySelector("#clearWeeklyDate").addEventListener("click", () => {
  document.querySelector("#weeklyDateStart").value = "";
  document.querySelector("#weeklyDateEnd").value = "";
  loadWeekly().catch((error) => showToast(error.message));
});

summaryDateStart.addEventListener("change", () =>
  loadSummaries().catch((error) => showToast(error.message)),
);
summaryDateEnd.addEventListener("change", () =>
  loadSummaries().catch((error) => showToast(error.message)),
);
clearDateFilterButton.addEventListener("click", () => {
  summaryDateStart.value = "";
  summaryDateEnd.value = "";
  loadSummaries().catch((error) => showToast(error.message));
});

async function runSummary(mode = "pending") {
  runSummaryButton.disabled = true;
  rerunSummaryButton.disabled = true;
  summaryLimit.disabled = true;
  summaryOutput.textContent = "正在调用大模型生成总结...";
  const limit = Math.min(Math.max(Number(summaryLimit.value || 100), 1), 500);

  try {
    const data = await requestJson("/api/summaries/run", {
      method: "POST",
      body: JSON.stringify({
        send_to_wechat: sendWechat.checked,
        mode,
        limit,
        groups: selectedGroupNames(),
      }),
    });
    summaryOutput.innerHTML = renderMarkdown(data.summary);
    if (data.push_error) {
      showToast(`总结已生成，但推送失败：${data.push_error}`);
    } else {
      showToast(sendWechat.checked ? "总结已生成并推送" : "总结已生成");
    }
    await loadFeedbacks();
    await loadSummaries();
  } catch (error) {
    summaryOutput.textContent = error.message;
    showToast(error.message);
  } finally {
    runSummaryButton.disabled = false;
    rerunSummaryButton.disabled = false;
    summaryLimit.disabled = false;
  }
}

runSummaryButton.addEventListener("click", () => runSummary("pending"));
rerunSummaryButton.addEventListener("click", () => runSummary("recent"));
autoInterval.addEventListener("change", () => saveInterval().catch((error) => showToast(error.message)));
importNowButton.addEventListener("click", () => importNow().catch((error) => showToast(error.message)));
syncGroupsButton.addEventListener("click", () => syncGroups().catch((error) => showToast(error.message)));
saveGroupsButton.addEventListener("click", () => saveGroups().catch((error) => showToast(error.message)));

refreshHomeStats().catch((error) => showToast(error.message));
loadSettings().catch((error) => showToast(error.message));
loadGroups().catch((error) => showToast(error.message));
loadSummaries().catch((error) => showToast(error.message));
loadWeekly().catch((error) => showToast(error.message));
loadChatHistory().catch((error) => showToast(error.message));
