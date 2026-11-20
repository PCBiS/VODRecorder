"use strict";
// 로그 보기 화면(텍스트 뷰어). 뼈대(app.js)의 공통 함수를 쓰고 registerView로 자기 화면을 등록한다.
// 서버(GET /api/logs)는 새것부터 한 쪽씩 준다. 화면은 로그 파일처럼 오래된 줄을 위, 새 줄을 아래에 둔다.
// 위쪽 "더 오래된 줄 보기"는 older 자리표로, 새 줄은 주기 갱신 때 newer 자리표로 받아 아래에 붙인다.
// 서버에서 온 글은 모두 textContent로만 넣는다.

const LOG_PAGE = 200;
const LOG_MAX_ROWS = 2000; // 화면에 둘 줄의 상한. 넘으면 처음부터 다시 읽는다
const LOG_LEVEL_TEXT = { DEBUG: "DEBUG", INFO: "INFO ", WARNING: "WARN ", ERROR: "ERROR", CRITICAL: "CRIT " };
const LOG_LEVEL_CLASS = { DEBUG: "debug", WARNING: "warn", ERROR: "bad", CRITICAL: "bad" };
const LOG_NEAR_BOTTOM = 40; // 맨 아래에서 이만큼(px) 안이면 새 줄을 따라 내려간다

const logState = { older: null, newer: null, rows: 0, generation: 0, busy: false, channels: new Map(), typing: null };

function logQuery(extra) {
  const q = new URLSearchParams({ limit: String(LOG_PAGE) });
  const level = $("logs-level").value;
  const channel = $("logs-channel").value;
  const text = $("logs-q").value.trim();
  if (level) q.set("level", level);
  if (channel) q.set("channel", channel);
  if (text) q.set("q", text);
  for (const [key, value] of Object.entries(extra || {})) q.set(key, value);
  return q;
}

function logValue(v) {
  if (typeof v === "string") return /[\s"=]/.test(v) || v === "" ? JSON.stringify(v) : v;
  return JSON.stringify(v);
}

// 한 줄: 시각 수준 출처 [이벤트] [채널] 문장 key=value… (오류 내용은 그 아래 들여 쓴 줄)
function logLine(e) {
  const level = String(e.level || "");
  const name = e.channel_id ? logState.channels.get(String(e.channel_id).toLowerCase()) : null;
  const source = String(e.logger || "").replace(/^live_recorder\.?/, "") || "main";
  const line = el("div", { class: `logline ${LOG_LEVEL_CLASS[level] || ""}`.trim(), dataset: { level } },
    el("span", { class: "lt", text: formatWhen(e.ts, { seconds: true }) }), " ",
    el("span", { class: "ll", text: LOG_LEVEL_TEXT[level] || level.padEnd(5) }), " ",
    el("span", { class: "ls", text: source }), " ");
  if (e.event) line.append(el("span", { class: "le", text: `[${e.event}]` }), " ");
  if (e.channel_id) line.append(el("span", { class: "lc", text: `<${name || e.channel_id}>` }), " ");
  line.append(el("span", { class: "lm", text: e.msg || "" }));
  const fields = Object.entries(e.fields || {});
  if (fields.length) line.append(" ", el("span", { class: "lf", text: fields.map(([k, v]) => `${k}=${logValue(v)}`).join(" ") }));
  if (e.exc) line.append(el("span", { class: "lx", text: `\n${String(e.exc).replace(/^/gm, "    ")}` }));
  return line;
}

function nearBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight <= LOG_NEAR_BOTTOM;
}

function showLogStatus(data) {
  const notes = [];
  if (data && data.stopped) notes.push("맞는 줄이 드물어 일부만 찾았습니다. 더 오래된 줄 보기로 이어서 찾습니다.");
  if (data && data.bad_lines) notes.push(`읽지 못한 줄 ${data.bad_lines}개는 건너뛰었습니다.`);
  if (!logState.older && logState.rows) notes.push("로그 파일의 처음입니다.");
  $("logs-status").textContent = notes.join(" ");
  $("logs-older").hidden = !logState.older;
}

function logEmpty(text) {
  return el("div", { class: "logs-empty muted", text });
}

async function loadChannelNames() {
  const data = await api("GET", "/api/channels").catch(() => null);
  if (!data) return;
  const select = $("logs-channel");
  const keep = select.value;
  logState.channels = new Map(data.channels.map((c) => [String(c.channel_id).toLowerCase(), c.name]));
  select.replaceChildren(el("option", { value: "", text: "모든 채널" }),
    ...data.channels.map((c) => el("option", { value: c.channel_id, text: c.name || c.channel_id })));
  select.value = data.channels.some((c) => c.channel_id === keep) ? keep : "";
}

// 첫 쪽부터 다시 읽는다(화면을 열 때, 거르기를 바꿀 때). 다 읽으면 맨 아래(가장 새 줄)를 보인다
async function reloadLogs() {
  const generation = ++logState.generation;
  logState.busy = true;
  $("logs-note").textContent = "읽는 중…";
  try {
    const data = await api("GET", `/api/logs?${logQuery()}`);
    if (generation !== logState.generation) return;
    logState.older = data.older;
    logState.newer = data.newer;
    logState.rows = data.entries.length;
    const box = $("logs-body");
    box.replaceChildren(...(data.entries.length ? data.entries.slice().reverse().map(logLine)
      : [logEmpty(data.files ? "맞는 줄이 없습니다." : "아직 로그 파일이 없습니다.")]));
    box.scrollTop = box.scrollHeight;
    $("logs-note").textContent = `${formatWhen(Date.now(), { seconds: true })}에 읽음`;
    showLogStatus(data);
  } catch (e) {
    if (generation === logState.generation) { $("logs-note").textContent = ""; toast(e.message, true); }
  } finally {
    if (generation === logState.generation) logState.busy = false;
  }
}

// 위쪽에 더 오래된 줄을 붙인다. 보던 자리는 그대로 둔다
async function olderLogs() {
  if (!logState.older || logState.busy) return;
  const generation = logState.generation;
  logState.busy = true;
  $("logs-older").disabled = true;
  try {
    const data = await api("GET", `/api/logs?${logQuery({ before: logState.older })}`);
    if (generation !== logState.generation) return;
    const box = $("logs-body");
    if (!logState.rows) box.replaceChildren();
    const height = box.scrollHeight;
    box.prepend(...data.entries.slice().reverse().map(logLine));
    box.scrollTop += box.scrollHeight - height;
    logState.rows += data.entries.length;
    logState.older = data.older;
    if (!logState.rows) box.replaceChildren(logEmpty("맞는 줄이 없습니다."));
    showLogStatus(data);
  } catch (e) {
    toast(e.message, true);
  } finally {
    $("logs-older").disabled = false;
    if (generation === logState.generation) logState.busy = false;
  }
}

// 주기 갱신: "새 줄 자동으로 보기"가 켜져 있으면 newer 뒤에 쓰인 줄을 아래에 붙인다.
// 맨 아래를 보고 있었으면 따라 내려가고, 위쪽을 읽는 중이면 자리를 그대로 둔다
async function newerLogs() {
  if (!$("logs-follow").checked || !logState.newer || logState.busy) return;
  const generation = logState.generation;
  logState.busy = true;
  let again = false;
  try {
    const data = await api("GET", `/api/logs?${logQuery({ after: logState.newer })}`);
    if (generation !== logState.generation) return;
    if (data.reset || data.more || logState.rows + data.entries.length > LOG_MAX_ROWS) { again = true; return; }
    logState.newer = data.newer;
    if (!data.entries.length) return;
    const box = $("logs-body");
    const follow = nearBottom(box);
    if (!logState.rows) box.replaceChildren();
    box.append(...data.entries.slice().reverse().map(logLine));
    logState.rows += data.entries.length;
    if (follow) box.scrollTop = box.scrollHeight;
    $("logs-note").textContent = `${formatWhen(Date.now(), { seconds: true })}에 새 줄 ${data.entries.length}개`;
  } catch (e) {
    /* 주기 갱신의 실패는 다음 주기에 다시 한다 */
  } finally {
    if (generation === logState.generation) logState.busy = false;
    if (again && generation === logState.generation) reloadLogs(); // 순환됐거나 새 줄이 너무 많다
  }
}

async function loadLogs() {
  await loadChannelNames();
  await reloadLogs();
}

function applyLogWrap() {
  $("logs-body").classList.toggle("nowrap", !$("logs-wrap").checked);
}

function bindLogs() {
  $("logs-filter").addEventListener("submit", (e) => { e.preventDefault(); reloadLogs(); });
  $("logs-level").addEventListener("change", reloadLogs);
  $("logs-channel").addEventListener("change", reloadLogs);
  $("logs-q").addEventListener("input", () => {
    clearTimeout(logState.typing);
    logState.typing = setTimeout(reloadLogs, 400);
  });
  $("logs-follow").addEventListener("change", () => { if ($("logs-follow").checked) newerLogs(); });
  $("logs-wrap").addEventListener("change", applyLogWrap);
  $("logs-bottom").addEventListener("click", () => { const box = $("logs-body"); box.scrollTop = box.scrollHeight; });
  $("logs-refresh").addEventListener("click", loadLogs);
  $("logs-older").addEventListener("click", olderLogs);
  applyLogWrap();
}

registerView("logs", { load: loadLogs, tick: newerLogs, bind: bindLogs });
