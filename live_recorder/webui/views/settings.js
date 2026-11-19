"use strict";
// 설정 화면. 하위 탭, 파일 이름 토큰, 시간대 규칙, 로그 내려받기, 확장 켜고 끄기. 뼈대(app.js)의 공통 함수를 쓰고 registerView로 자기 화면을 등록한다.

let settingsLoaded = null;

// ---- 설정 ----
// ---- 설정 (하위 탭. 탭마다 그 탭의 설정 키만 저장한다) ----
// 하위 탭은 화면에 있는 것(중심부 넷과 켜진 확장이 넣은 것)을 순서대로 쓴다. 시작할 때 채운다
const SETTINGS_PARTS = [];
const PART_FORMS = { record: "settings-form", notify: "settings-notify-form",
  account: "settings-account-form", general: "settings-general-form" };
// 확장이 넣은 하위 탭(예: downloads의 "다시보기"): { form, values(f), fill(f, s), load(), bind() }
const SETTINGS_EXT_PARTS = {};

function registerSettingsPart(name, spec) {
  SETTINGS_EXT_PARTS[name] = spec;
  PART_FORMS[name] = spec.form;
}
let settingsEvents = [];
let patternProblem = null; // 저장된 패턴을 쓸 수 없어 기본 패턴을 쓰는 중이면 {stored, reason}
let settingsPart = "record";
// 탭마다 화면을 채울 때 쓴 값. 저장할 때는 사용자가 이 값에서 바꾼 칸만 보낸다(그 사이 서버에서 바뀐 다른 값을 덮지 않는다)
const partBase = {};

function gib(bytes) {
  return +(bytes / 1024 ** 3).toFixed(2);
}

// 탭마다 화면 칸 → 설정 키. 화면에 없는 설정은 다루지 않는다
function partValues(part) {
  const f = $(PART_FORMS[part]);
  if (SETTINGS_EXT_PARTS[part]) return SETTINGS_EXT_PARTS[part].values(f);
  if (part === "record") {
    return {
      min_free_bytes: Math.round(Number(f.min_free_gib.value) * 1024 ** 3),
      timemachine_enabled: f.timemachine_enabled.checked,
      rewind_seconds: Number(f.rewind_seconds.value),
      remux_enabled: f.remux_enabled.checked,
      delete_original: f.delete_original.checked,
      filename_pattern: f.filename_pattern.value,
      filename_date_format: f.filename_date_format.value,
      filename_time_format: f.filename_time_format.value,
    };
  }
  if (part === "notify") {
    const out = { notify_enabled: f.notify_enabled.checked, notify_dedupe_seconds: Number(f.notify_dedupe_seconds.value) };
    for (const box of $("event-list").querySelectorAll("input[type=checkbox]")) out[box.name] = box.checked;
    return out;
  }
  if (part === "account") {
    return {
      session_minutes: Number(f.session_minutes.value),
      login_limit_enabled: f.login_limit_enabled.checked,
      trusted_proxies: lines(f.trusted_proxies.value),
    };
  }
  return {
    display_date_format: f.display_date_format.value,
    display_time_format: f.display_time_format.value,
    fm_enabled: f.fm_enabled.checked,
    fm_readonly: f.fm_readonly.checked,
    fm_trash: f.fm_trash.checked,
    fm_show_hidden: f.fm_show_hidden.checked,
    fm_roots: lines(f.fm_roots.value),
  };
}

function fillPart(part, s) {
  partBase[part] = s;
  const f = $(PART_FORMS[part]);
  if (SETTINGS_EXT_PARTS[part]) {
    SETTINGS_EXT_PARTS[part].fill(f, s);
  } else if (part === "record") {
    f.min_free_gib.value = gib(s.min_free_bytes);
    f.timemachine_enabled.checked = s.timemachine_enabled;
    f.rewind_seconds.value = s.rewind_seconds;
    f.remux_enabled.checked = s.remux_enabled;
    f.delete_original.checked = s.delete_original;
    // 저장된 패턴을 쓸 수 없으면 칸에는 저장된 값을 보이고 기본 패턴을 쓰고 있다고 알린다
    f.filename_pattern.value = patternProblem ? patternProblem.stored : s.filename_pattern;
    if (patternProblem) partBase[part] = { ...s, filename_pattern: patternProblem.stored };
    const note = $("filename-pattern-problem");
    note.hidden = !patternProblem;
    note.textContent = patternProblem ? `모르는 토큰이 있어 기본 패턴을 쓰고 있습니다(${patternProblem.reason}). 기본 패턴: ${s.filename_pattern}` : "";
    f.filename_date_format.value = s.filename_date_format;
    f.filename_time_format.value = s.filename_time_format;
    showFilenameExample();
  } else if (part === "notify") {
    f.notify_enabled.checked = s.notify_enabled;
    f.notify_dedupe_seconds.value = s.notify_dedupe_seconds;
    $("event-list").replaceChildren(...settingsEvents.map((ev) => {
      const box = el("input", { type: "checkbox", name: ev.key });
      box.checked = Boolean(s[ev.key]);
      return el("label", { class: "check" }, box, ev.label, ev.required ? el("span", { class: "req", text: "필수" }) : null);
    }));
  } else if (part === "account") {
    f.session_minutes.value = s.session_minutes;
    f.login_limit_enabled.checked = s.login_limit_enabled;
    f.trusted_proxies.value = s.trusted_proxies.join("\n");
  } else {
    f.display_date_format.value = s.display_date_format;
    f.display_time_format.value = s.display_time_format;
    showDisplayExample();
    for (const key of ["fm_enabled", "fm_readonly", "fm_trash", "fm_show_hidden"]) f[key].checked = s[key];
    f.fm_roots.value = s.fm_roots.join("\n");
  }
}

// 고른 모양과 지금의 패턴으로 서버가 실제 규칙대로 만든 예시 이름
let filenameExampleTimer = null;
function showFilenameExample() {
  showTokenTable();
  clearTimeout(filenameExampleTimer);
  filenameExampleTimer = setTimeout(async () => {
    const f = $("settings-form");
    const q = new URLSearchParams({ pattern: f.filename_pattern.value, date_format: f.filename_date_format.value,
      time_format: f.filename_time_format.value });
    const box = $("filename-example");
    try {
      const res = await api("GET", `/api/settings/filename-example?${q}`);
      box.textContent = res.example;
      box.classList.remove("bad-text");
    } catch (e) {
      box.textContent = e.message;
      box.classList.add("bad-text");
    }
  }, 250);
}

// 토큰 설명 표. 토큰 목록과 예시 값은 서버(naming.TOKEN_HELP)에서 받는다. 날짜·시각 모양을 바꾸면 다시 받는다
let tokenShapes = "";
async function showTokenTable() {
  const f = $("settings-form");
  const key = `${f.filename_date_format.value}|${f.filename_time_format.value}`;
  if (key === tokenShapes) return;
  tokenShapes = key;
  const q = new URLSearchParams({ date_format: f.filename_date_format.value, time_format: f.filename_time_format.value });
  const data = await act(() => api("GET", `/api/settings/filename-tokens?${q}`));
  if (!data) { tokenShapes = ""; return; }
  $("token-rows").replaceChildren(...data.tokens.map((t) => el("div", { class: "token-row", role: "row" },
    el("span", { role: "cell" }, el("button", { class: "token-insert", type: "button", text: `{${t.token}}`,
      "aria-label": `{${t.token}} 넣기`, onclick: () => insertToken(t.token) })),
    el("span", { role: "cell", text: t.meaning }),
    el("span", { role: "cell" }, el("code", { text: t.example })))));
}

// 패턴 칸의 커서 자리에 토큰을 넣는다. 저장하지 않은 변경이 된다
function insertToken(name) {
  const input = $("settings-form").filename_pattern;
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? input.value.length;
  input.setRangeText(`{${name}}`, start, end, "end");
  input.focus();
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

// 고른 화면 모양으로 지금 시각을 보인다(저장하기 전에도 바로)
function showDisplayExample() {
  const f = $("settings-general-form");
  const shapes = { date: f.display_date_format.value, time: f.display_time_format.value };
  $("display-example").textContent = `${formatWhen(Date.now(), { shapes })} · 초를 더하면 ${formatWhen(Date.now(), { shapes, seconds: true })}`;
}

// only를 주면 그 탭과 고치던 것이 없는 탭만 새 값으로 채운다(다른 탭에서 고치던 칸은 그대로 둔다)
function fillSettings(payload, only) {
  const keep = only ? SETTINGS_PARTS.filter((part) => part !== only && partDirty(part)) : [];
  settingsLoaded = payload.settings;
  settingsEvents = payload.events;
  patternProblem = payload.filename_pattern_problem || null;
  setDisplay({ date: payload.settings.display_date_format, time: payload.settings.display_time_format }); // 저장된 화면 모양
  for (const part of SETTINGS_PARTS) if (!keep.includes(part)) fillPart(part, payload.settings);
  setBadge($("tg-token"), payload.telegram.bot_token, "설정됨", "없음");
  setBadge($("tg-chat"), payload.telegram.chat_id, "설정됨", "없음");
}

// 그 탭에서 사용자가 바꾼 값만 돌려준다
function collectSettings(part) {
  const changes = {};
  const base = partBase[part] || settingsLoaded;
  for (const [key, value] of Object.entries(partValues(part))) {
    const before = base[key];
    const same = Array.isArray(value) ? JSON.stringify(before) === JSON.stringify(value) : before === value;
    if (!same) changes[key] = value;
  }
  return changes;
}

// 비밀 칸(텔레그램 봇, 비밀번호 변경)은 무엇이든 적혀 있으면 저장하지 않은 변경이다
const PART_SECRET_FORMS = { notify: "telegram-form", account: "password-form" };

function partDirty(part) {
  if (!settingsLoaded) return false;
  if (Object.keys(collectSettings(part)).length) return true;
  const secret = PART_SECRET_FORMS[part];
  return Boolean(secret && [...$(secret).querySelectorAll("input")].some((i) => (i.type === "checkbox" ? i.checked : i.value)));
}

function discardPart(part) {
  if (settingsLoaded) fillPart(part, settingsLoaded); // 버리면 가장 최근에 받은 값으로 채운다
  if (PART_SECRET_FORMS[part]) $(PART_SECRET_FORMS[part]).reset();
}

// 저장하지 않은 변경이 있으면 확인을 받는다. 버리기로 하면 그 탭을 저장된 값으로 되돌린다
function confirmLeavePart(part) {
  if (!partDirty(part)) return true;
  if (!confirm("저장하지 않은 변경이 있습니다. 버리고 옮길까요?")) return false;
  discardPart(part);
  return true;
}

function settingsHash() {
  return `#settings/${settingsPart}`;
}

function selectSettingsPart(part, opts) {
  const { force = false, focus = false } = opts || {};
  if (!SETTINGS_PARTS.includes(part)) part = "record";
  if (!force && part !== settingsPart && !confirmLeavePart(settingsPart)) {
    if (focus) $(`stab-${settingsPart}`).focus();
    return false;
  }
  settingsPart = part;
  for (const name of SETTINGS_PARTS) {
    const tab = $(`stab-${name}`);
    tab.setAttribute("aria-selected", String(name === part));
    tab.setAttribute("tabindex", name === part ? "0" : "-1");
    $(`spanel-${name}`).hidden = name !== part;
  }
  if (currentView === "settings") history.replaceState(null, "", location.pathname + settingsHash());
  if (focus) $(`stab-${part}`).focus();
  return true;
}

function bindSettingsTabs() {
  SETTINGS_PARTS.push(...[...document.querySelectorAll("[data-stab]")].map((tab) => tab.dataset.stab));
  for (const spec of Object.values(SETTINGS_EXT_PARTS)) if (spec.bind) spec.bind();
  for (const name of SETTINGS_PARTS) $(`stab-${name}`).addEventListener("click", () => selectSettingsPart(name));
  const rf = $("settings-form");
  for (const name of ["filename_pattern", "filename_date_format", "filename_time_format"]) {
    rf[name].addEventListener(name === "filename_pattern" ? "input" : "change", showFilenameExample);
  }
  const gf = $("settings-general-form");
  for (const name of ["display_date_format", "display_time_format"]) gf[name].addEventListener("change", showDisplayExample);
  // 좌우 화살표·Home·End로 탭을 옮긴다(WAI-ARIA 탭 패턴)
  $("settings-tabs").addEventListener("keydown", (event) => {
    const i = SETTINGS_PARTS.indexOf(settingsPart);
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: SETTINGS_PARTS.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    selectSettingsPart(SETTINGS_PARTS[(next + SETTINGS_PARTS.length) % SETTINGS_PARTS.length], { focus: true });
  });
  for (const part of SETTINGS_PARTS) {
    $(PART_FORMS[part]).addEventListener("submit", async (event) => {
      event.preventDefault();
      const changes = collectSettings(part);
      if (!Object.keys(changes).length) { toast("바뀐 값이 없습니다"); return; }
      const payload = await act(() => api("PATCH", "/api/settings", changes), "설정을 저장했습니다");
      if (payload) fillSettings(payload, part);
    });
  }
  // 저장하지 않은 변경이 있는 채 창을 닫거나 새로고침하면 브라우저가 묻는다
  window.addEventListener("beforeunload", (event) => {
    if (currentView === "settings" && SETTINGS_PARTS.some(partDirty)) { event.preventDefault(); event.returnValue = ""; }
  });
}

async function loadSettings() {
  const payload = await act(() => api("GET", "/api/settings"));
  if (payload) fillSettings(payload);
  showOptIn();
  showGates();
  loadTools();
  loadApiKeys();
  for (const name of SETTINGS_PARTS) if (SETTINGS_EXT_PARTS[name] && SETTINGS_EXT_PARTS[name].load) SETTINGS_EXT_PARTS[name].load();
}

// ---- 접근 키 ----
const APIKEY_CONFIRM = {
  view: "이 키를 가진 프로그램은 화면 없이 채널 상태·기록·다운로드 작업·파일 목록을 읽고 녹화 파일을 내려받을 수 있습니다. 발급할까요?",
  operate: "이 키를 가진 프로그램은 화면 없이 녹화를 시작·중지하고 채널을 더하거나 지울 수 있습니다. 다운로드 작업도 더하고 취소할 수 있습니다. 발급할까요?",
};

async function loadApiKeys() {
  const info = await api("GET", "/api/keys").catch(() => null);
  const group = $("apikeys-group");
  group.hidden = !(info && info.enabled);
  if (group.hidden) return;
  const when = (v) => (v ? formatWhen(v) : "");
  $("apikeys-table").tBodies[0].replaceChildren(...info.keys.map((k) => el("tr", {},
    el("td", { text: k.name }), el("td", { text: k.scope_label }), el("td", {}, el("code", { text: k.prefix })),
    el("td", { text: when(k.created_at) }), el("td", { text: k.expires_at ? when(k.expires_at) + (k.expired ? " (만료됨)" : "") : "없음" }),
    el("td", { text: k.last_used_at ? `${when(k.last_used_at)} (${k.uses}번)` : "쓴 적 없음" }),
    el("td", {}, el("button", { class: "btn danger", type: "button", text: "폐기", onclick: () => revokeApiKey(k) })))));
  $("apikeys-form").querySelector("button[type=submit]").disabled = info.keys.length >= info.max;
  $("apikeys-result").textContent = info.keys.length >= info.max ? `키는 ${info.max}개까지 만들 수 있습니다.` : "";
}

async function issueApiKey(event) {
  event.preventDefault();
  const form = event.target;
  const body = { name: form.name.value.trim(), scope: form.scope.value, expiry: form.expiry.value };
  if (!confirm(APIKEY_CONFIRM[body.scope])) return;
  const res = await act(() => api("POST", "/api/keys", body), "접근 키를 발급했습니다");
  if (!res) return;
  form.reset();
  $("apikeys-value").textContent = res.value; // 이 화면을 떠나면 다시 볼 수 없다
  $("apikeys-new").hidden = false;
  loadApiKeys();
}

async function revokeApiKey(k) {
  if (!confirm(`접근 키 "${k.name}"을(를) 폐기할까요? 이 키를 쓰는 프로그램은 곧바로 접근할 수 없게 됩니다.`)) return;
  if (await act(() => api("DELETE", `/api/keys/${encodeURIComponent(k.id)}`), "접근 키를 폐기했습니다")) loadApiKeys();
}

function copyApiKey() {
  // 값을 고른 뒤 브라우저의 복사 명령을 쓴다(되지 않으면 골라 둔 채로 두어 직접 복사하게 한다)
  const range = document.createRange();
  range.selectNodeContents($("apikeys-value"));
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  if (ok) toast("복사했습니다"); else toast("값을 골라 두었습니다. 직접 복사해 주십시오", true);
}

// ---- 로그 내려받기 ----
// "더 가리기"를 켤 때 한 번 묻는다. 취소하면 꺼진 채로 둔다. 끌 때는 묻지 않는다
const STRICT_EXPORT_CONFIRM = "더 가리기를 켜면 로그에서 채널·제목·파일 이름과 문장 같은 값이 빠져 문제 분석이 어려워집니다. 그래도 켤까요?";

function confirmStrictExport() {
  const box = $("export-strict");
  if (box.checked && !confirm(STRICT_EXPORT_CONFIRM)) box.checked = false;
}

async function exportLogs(event) {
  event.preventDefault();
  const button = $("export-button");
  const result = $("export-result");
  button.disabled = true;
  result.textContent = "묶는 중…";
  try {
    const resp = await fetch("/api/logs/export", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ range: $("export-range").value, strict: $("export-strict").checked }),
    });
    if (resp.status === 401) { leavingPage(); location.replace("/login"); return; }
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || data.detail || `요청 실패 (${resp.status})`);
    }
    const blob = await resp.blob();
    const name = (/filename="([^"]+)"/.exec(resp.headers.get("Content-Disposition") || "") || [])[1] || document.title.replace(/\s+/g, "").toLowerCase() + "-logs.zip"; // 대체값: 표시 이름(문서 제목)에서 만든다
    const url = URL.createObjectURL(blob);
    const link = el("a", { href: url, download: name });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    result.textContent = `${name} (${bytes(blob.size)})`;
  } catch (e) {
    result.textContent = "";
    toast(e.message, true);
  } finally {
    button.disabled = false;
  }
}

// ---- 플랫폼별 확인 간격 ----
async function showGates() {
  try { // 지금 채널 수로 셈한 한 바퀴 시간을 보이려고 새로 받는다
    const info = await api("GET", "/api/platforms");
    if (info && Array.isArray(info.platforms)) platforms = info.platforms;
  } catch (e) { /* 받지 못하면 처음 받은 값으로 보인다 */ }
  const list = usablePlatforms().filter((p) => p.gate);
  $("gate-group").hidden = list.length === 0;
  $("gate-rows").replaceChildren(...list.map(gateRow));
}

function gateRow(p) {
  const g = p.gate;
  const scan = el("input", { type: "number", min: "10", max: "3600", step: "1", "aria-label": `${p.name} 스캔 주기(초)` });
  const gap = el("input", { type: "number", min: "1", max: "600", step: "0.5", "aria-label": `${p.name} 채널 사이 간격(초)` });
  scan.value = g.scan;
  gap.value = g.gap;
  const warn = (text) => el("p", { class: "form-error field-note", text, hidden: !text });
  const scanWarn = warn(g.warnings.scan);
  const gapWarn = warn(g.warnings.gap);
  const cycle = el("p", { class: "muted field-note", text: `지금 감시 중인 채널 ${g.channels}개: 한 바퀴 약 ${g.cycle}초${g.note ? ` · ${g.note}` : ""}` });
  const result = el("span", { class: "muted" });
  const save = el("button", { class: "btn small", type: "button", text: "저장", onclick: async () => {
    const body = { scan: Number(scan.value), gap: Number(gap.value) };
    // 경고 범위로 저장하려 하면 확인을 받는다
    if ((body.scan <= 30 || body.gap < 5) && !confirm(p.gate_warning)) return;
    const res = await act(() => api("PUT", `/api/platforms/${encodeURIComponent(p.id)}/gate`, body), "다음 바퀴부터 적용합니다");
    if (res) { p.gate = res; showGates(); }
  } });
  return el("div", { class: "gate-row", dataset: { gate: p.id } },
    el("h3", { text: p.name }),
    el("div", { class: "grid" }, el("label", {}, "스캔 주기(초, 10~3600)", scan, scanWarn),
      el("label", {}, "채널 사이 간격(초, 1~600)", gap, gapWarn)),
    cycle, el("div", { class: "row" }, save, result));
}

// ---- 켜야 쓰는 플랫폼 ----
// 켜고 끄는 플랫폼은 "플랫폼" 묶음 하나에 나란히 보인다
function showOptIn() {
  const list = platforms.filter((p) => p.opt_in);
  const box = $("optin-group");
  box.hidden = list.length === 0;
  box.replaceChildren(el("section", { class: "panel", id: "platforms-panel" }, el("h2", { text: "플랫폼" }),
    ...list.map(optInRow)));
}

function optInRow(p) {
  const n = p.notice || {};
  const result = el("span", { class: "muted", dataset: { result: "" } });
  const status = p.enabled ? "켜져 있습니다" : (n.blocked ? `켤 수 없습니다: ${n.blocked}` : "꺼져 있습니다");
  const enable = el("button", { class: "btn primary", type: "button", text: n.enable_label || "사용",
    onclick: () => openOptInNotice(p, result) });
  const disable = el("button", { class: "btn", type: "button", text: n.disable_label || "끄기",
    onclick: async () => {
      if (!confirm(n.disable_confirm || "끌까요?")) return;
      const r = await act(() => api("POST", `/api/platforms/${encodeURIComponent(p.id)}/disable`));
      if (r) location.reload(); // 플랫폼 목록이 바뀌었으므로 화면을 새로 그린다
    } });
  return el("div", { class: "optin-row", dataset: { optin: p.id } },
    el("h3", { text: p.name }),
    el("p", { class: "muted", text: status }),
    el("div", { class: "row" }, p.enabled ? disable : (n.blocked ? null : enable), result));
}

// 고지 창. 동의하면 서버가 자체 점검을 하고 통과해야 켜진다
function openOptInNotice(p, result) {
  const n = p.notice || {};
  const dialog = el("dialog", { class: "notice-dialog", "aria-label": n.title || p.name });
  const agree = el("button", { class: "btn primary", type: "button", text: n.agree_label || "동의하고 켜기" });
  const cancel = el("button", { class: "btn", type: "button", text: "취소", onclick: () => dialog.close() });
  const error = el("p", { class: "form-error", role: "alert", hidden: true });
  agree.addEventListener("click", async () => {
    agree.disabled = true;
    error.hidden = true;
    result.textContent = "점검 중…";
    try {
      await api("POST", `/api/platforms/${encodeURIComponent(p.id)}/enable`, { agree: true });
      dialog.close();
      location.reload();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
      result.textContent = "";
      agree.disabled = false;
    }
  });
  dialog.append(el("h2", { text: n.title || p.name }),
    el("ul", {}, ...(n.lines || []).map((line) => el("li", { text: line }))),
    error, el("div", { class: "row end" }, cancel, agree));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

// ---- 외부 도구. 지금 쓰는 판과 출처, 기본 판, 받아 두고 아직 적용되지 않은 판, 마지막 갱신 ----
async function loadTools() {
  let data;
  try { data = await api("GET", "/api/tools"); } catch (e) { return; }
  const box = $("tools-group");
  const tools = (data && data.tools) || [];
  box.hidden = tools.length === 0;
  box.replaceChildren(...tools.map(toolPanel));
}

function toolPanel(t) {
  const kv = (label, value) => el("div", { class: "kv" }, el("span", { text: label }), el("span", { text: value || "—" }));
  const last = t.last_update
    ? `${formatWhen(t.last_update.at, { seconds: true })} · ${t.last_update.message || t.last_update.result}` : "";
  const result = el("span", { class: "muted", dataset: { result: "" } });
  const buttons = (t.actions || []).map((a) => el("button", {
    class: a.id === "update" ? "btn primary" : "btn", type: "button", text: a.label, disabled: t.running || null,
    onclick: async (event) => {
      if (!confirm(a.confirm)) return;
      event.target.disabled = true;
      result.textContent = a.id === "update" ? "받는 중…(1분쯤 걸릴 수 있습니다)" : "되돌리는 중…";
      const r = await act(() => api("POST", `/api/tools/${encodeURIComponent(t.name)}/${a.id}`));
      result.textContent = r ? r.message : "";
      loadTools();
    },
  }));
  return el("section", { class: "panel", dataset: { tool: t.name } },
    el("h2", { text: t.name }),
    el("div", { class: "kvs" },
      kv("지금 쓰는 판", t.version ? `${t.version} (${t.source})` : t.source),
      kv("설치된 기본 판", t.default_version),
      kv("받아 두고 아직 적용되지 않은 판", t.pending ? `${t.pending} · 다음 서버 시작부터 적용됩니다` : ""),
      kv("마지막 갱신", last)),
    t.note ? el("p", { class: "muted", text: t.note }) : null,
    t.enabled ? null : el("p", { class: "muted", text: "교체 기능이 꺼져 있습니다(환경변수). 설치된 판만 씁니다." }),
    buttons.length ? el("div", { class: "row" }, ...buttons, result) : null);
}

// ---- 확장 켜고 끄기 ----
// 등록된 확장이 없으면 묶음을 보이지 않는다. 바꾸면 다음 서버 시작부터 먹는다
function showExtensions(info) {
  const list = (info && Array.isArray(info.extensions)) ? info.extensions : [];
  $("extensions-group").hidden = list.length === 0;
  if (!list.length) return;
  const titles = Object.fromEntries(list.map((x) => [x.name, x.title]));
  $("extensions-list").replaceChildren(...list.map((x) => {
    const box = el("input", { type: "checkbox", name: x.name });
    box.checked = x.enabled;
    // 필요한 확장이 꺼져 있으면 켤 수 없고, 다른 확장이 필요로 하는 확장을 끄면 함께 꺼진다고 묻는다
    box.addEventListener("change", () => onExtensionToggle(box, x, list, titles));
    const needs = x.requires.length ? `필요한 확장: ${x.requires.map((n) => titles[n] || n).join(", ")}` : "";
    const platforms = (x.requires_platforms || []).length ? `필요한 플랫폼: ${x.requires_platforms.join(", ")}` : ""; //
    return el("div", { class: "ext-row" },
      el("label", { class: "check" }, box, `${x.title} ${x.version}`),
      el("p", { class: "muted field-note", text: [x.description, needs, platforms].filter(Boolean).join(" ") }));
  }));
  $("extensions-note").textContent = info.restart_needed ? `${info.note}. 지금 서버에 적용된 것과 다릅니다.` : `${info.note}.`;
}

function extensionBox(name) {
  return $("extensions-list").querySelector(`input[name="${name}"]`);
}

function onExtensionToggle(box, ext, list, titles) {
  if (box.checked) {
    const missing = ext.requires.filter((n) => !(extensionBox(n) && extensionBox(n).checked));
    if (missing.length) {
      box.checked = false;
      toast(`${ext.title} 확장은 ${missing.map((n) => titles[n] || n).join(", ")} 확장이 켜져 있어야 켤 수 있습니다`, true);
    }
    return;
  }
  const dependents = [];
  const todo = [ext.name];
  while (todo.length) {
    const cur = todo.pop();
    for (const x of list) if (x.requires.includes(cur) && extensionBox(x.name).checked && !dependents.includes(x.name)) { dependents.push(x.name); todo.push(x.name); }
  }
  if (!dependents.length) return;
  if (!confirm(`${ext.title} 확장을 끄면 ${dependents.map((n) => titles[n] || n).join(", ")} 확장도 함께 꺼집니다. 끌까요?`)) { box.checked = true; return; }
  for (const name of dependents) extensionBox(name).checked = false;
}

async function saveExtensions(event) {
  event.preventDefault();
  const enabled = [...$("extensions-list").querySelectorAll("input")].filter((x) => x.checked).map((x) => x.name);
  const info = await act(() => api("PUT", "/api/extensions", { enabled }), "확장 설정을 저장했습니다. 다음 서버 시작부터 적용됩니다");
  if (info) { extensionsInfo = info; showExtensions(info); }
}

function bindSettings() {
  // 타임머신을 쓰는 플랫폼이 없으면 설정의 타임머신 묶음을 감춘다
  $("settings-timemachine").hidden = platforms.length > 0 && !platforms.some((p) => p.features.timemachine);
  // 되감기를 쓰지 않는 플랫폼(켜진 것)의 채널에는 되감기 설정이 적용되지 않는다
  const noRewind = usablePlatforms().filter((p) => !p.features.rewind && !p.features.timemachine).map((p) => p.name);
  $("rewind-note").textContent = noRewind.length ? `되감기는 ${noRewind.join("·")} 채널에는 적용되지 않습니다(재생목록이 짧아 라이브 끝에서 시작합니다).` : "";
  $("rewind-note").hidden = !noRewind.length;
  bindSettingsTabs();
  $("telegram-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const body = {};
    for (const key of ["bot_token", "chat_id"]) if (form[key].value.trim()) body[key] = form[key].value.trim();
    if (!Object.keys(body).length) { toast("바꿀 값을 입력하십시오", true); return; }
    const result = await act(() => api("PUT", "/api/notify/secret", body), "텔레그램 값을 저장했습니다");
    form.reset();
    if (result) {
      const payload = await act(() => api("GET", "/api/settings"));
      if (payload) fillSettings(payload, "notify"); // 다른 탭에서 고치던 값은 그대로 둔다
    }
  });
  $("tg-test").addEventListener("click", async () => {
    $("tg-result").textContent = "보내는 중…";
    const result = await act(() => api("POST", "/api/notify/test"));
    $("tg-result").textContent = result ? (result.ok ? "테스트 알림을 보냈습니다" : `실패: ${result.detail}`) : "";
  });
  $("export-form").addEventListener("submit", exportLogs);
  $("export-strict").addEventListener("change", confirmStrictExport);
  $("password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    if (form.new.value !== form.confirm.value) { toast("새 비밀번호가 서로 다릅니다", true); return; }
    const res = await act(() => api("POST", "/api/account/password", { current: form.current.value, new: form.new.value,
      revoke_keys: !!(form.revoke_keys && form.revoke_keys.checked) }));
    form.reset();
    if (res) { alert("비밀번호를 바꿨습니다. 다시 로그인하십시오."); location.replace("/login"); }
  });
  $("extensions-form").addEventListener("submit", saveExtensions);
  $("apikeys-form").addEventListener("submit", issueApiKey);
  $("apikeys-copy").addEventListener("click", copyApiKey);
  showExtensions(extensionsInfo);
}

registerView("settings", {
  load: loadSettings,
  bind: bindSettings,
  // 저장하지 않은 변경이 있으면 다른 메뉴로 가기 전에 확인을 받는다
  leave: () => confirmLeavePart(settingsPart),
  enter(again) {
    selectSettingsPart(settingsPart, { force: true });
    return !(again && SETTINGS_PARTS.some(partDirty)); // 고치던 값을 다시 읽어 덮지 않는다
  },
  hash: () => settingsHash(),
  fromHash(part, viaHashChange) {
    if (SETTINGS_PARTS.includes(part) && !(viaHashChange && partDirty(settingsPart))) settingsPart = part;
  },
  hashChange(part) {
    if (!selectSettingsPart(part)) history.replaceState(null, "", location.pathname + settingsHash());
  },
});
