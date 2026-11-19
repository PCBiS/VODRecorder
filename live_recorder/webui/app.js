"use strict";
// 화면 뼈대: 시작, 화면 전환, 공통 요청·날짜·알림 띠 함수. 화면별 로직은 views/*.js에 있다.
// 서버에서 온 문자열은 전부 textContent로만 넣는다.

const REFRESH_MS = 5000; // 상태를 주기적으로 갱신한다
let csrf = "";
let currentView = "channels";
// 플랫폼 정보(이름·선택 기능·채널 주소 규칙·자격 이름)는 서버(/api/platforms)가 준다. 화면에 플랫폼 주소를 적지 않는다
let platforms = [];
let defaultPlatform = null;
let timer = null;
let viewOnly = false; // 관리자 계정이 없으면 보기 전용이다

const $ = (id) => document.getElementById(id);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (value === true) node.setAttribute(key, "");
    else if (value !== false && value != null) node.setAttribute(key, value);
  }
  for (const child of children) if (child != null) node.append(child);
  return node;
}

function toast(message, isError) {
  const box = $("toast");
  box.textContent = message;
  box.className = isError ? "toast error" : "toast";
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, isError ? 6000 : 3000);
}

// 다른 화면으로 옮겨 가는 중에 시작 코드가 멈추면 실패 안내를 띄우지 않는다
function leavingPage() {
  if (window.lrBoot) window.lrBoot.leaving();
}

async function api(method, path, body) {
  const headers = {};
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, {
    method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 401) { leavingPage(); location.replace("/login"); throw new Error("로그인이 필요합니다"); }
  if (resp.status === 403 && !viewOnly) {
    const data = await resp.clone().json().catch(() => ({}));
    if (data.view_only) { leavingPage(); location.reload(); throw new Error(data.error); } // 계정이 지워져 보기 전용이 됐다
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.error || data.detail || `요청 실패 (${resp.status})`);
    err.status = resp.status;
    err.data = data; // 중복 거부의 영상 제목
    throw err;
  }
  return data;
}

async function act(fn, okMessage) {
  try {
    const result = await fn();
    if (okMessage) toast(typeof okMessage === "function" ? okMessage(result) : okMessage);
    return result;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

function hms(seconds) {
  if (seconds == null) return "";
  const s = Math.max(0, Math.floor(seconds));
  const pad = (n) => String(n).padStart(2, "0");
  return `${Math.floor(s / 3600)}:${pad(Math.floor(s % 3600 / 60))}:${pad(s % 60)}`;
}

// ---- 날짜·시각 ----
// 화면의 날짜·시각은 모두 이 함수로 만든다. 모양은 서버 설정(display_date_format·display_time_format)을 따르고 늘 Asia/Seoul이다
const DISPLAY_DATE_SHAPES = ["YYYY-MM-DD", "YYYY.MM.DD", "YYYY/MM/DD", "YY.MM.DD", "MM/DD", "M월 D일"];
const DISPLAY_TIME_SHAPES = ["HH:MM", "HH:MM:SS", "오전/오후 H:MM"];
const display = { date: "YYYY-MM-DD", time: "HH:MM" };
const KST_OFFSET_MS = 9 * 3600000;

function setDisplay(shapes) {
  if (shapes && DISPLAY_DATE_SHAPES.includes(shapes.date)) display.date = shapes.date;
  if (shapes && DISPLAY_TIME_SHAPES.includes(shapes.time)) display.time = shapes.time;
}

// value: ISO 문자열, 밀리초, Date, 또는 시간대 없는 한국 시각 문자열("YYYY-MM-DD HH:MM:SS", 다시보기 게시 날짜)
// opts: date(날짜, 기본 참), time(시각, 기본 참), seconds(초를 더함), shapes(설정 대신 쓸 모양. 설정 화면의 예시용)
function formatWhen(value, opts) {
  const { date = true, time = true, seconds = false, shapes = display } = opts || {};
  let ms;
  if (typeof value === "number") ms = value;
  else if (value instanceof Date) ms = value.getTime();
  else if (typeof value === "string" && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?$/.test(value)) ms = Date.parse(value.replace(" ", "T") + "+09:00");
  else ms = Date.parse(value);
  if (value == null || value === "" || Number.isNaN(ms)) return "";
  const k = new Date(ms + KST_OFFSET_MS); // UTC 값으로 읽으면 한국 시각이다
  const pad = (n) => String(n).padStart(2, "0");
  const Y = k.getUTCFullYear(), M = k.getUTCMonth() + 1, D = k.getUTCDate();
  const h = k.getUTCHours(), mi = k.getUTCMinutes(), sec = k.getUTCSeconds();
  const days = {
    "YYYY-MM-DD": `${Y}-${pad(M)}-${pad(D)}`, "YYYY.MM.DD": `${Y}.${pad(M)}.${pad(D)}`, "YYYY/MM/DD": `${Y}/${pad(M)}/${pad(D)}`,
    "YY.MM.DD": `${pad(Y % 100)}.${pad(M)}.${pad(D)}`, "MM/DD": `${pad(M)}/${pad(D)}`, "M월 D일": `${M}월 ${D}일`,
  };
  const withSec = (text) => (seconds || shapes.time === "HH:MM:SS" ? `${text}:${pad(sec)}` : text);
  let clock;
  if (shapes.time === "오전/오후 H:MM") clock = `${h < 12 ? "오전" : "오후"} ${withSec(`${h % 12 || 12}:${pad(mi)}`)}`;
  else clock = withSec(`${pad(h)}:${pad(mi)}`);
  const parts = [];
  if (date) parts.push(days[shapes.date] || days["YYYY-MM-DD"]);
  if (time) parts.push(clock);
  return parts.join(" ");
}

function platformOf(id) {
  return platforms.find((p) => p.id === (id || defaultPlatform)) || platforms[0] || null;
}

// 지금 쓸 수 있는 플랫폼(켜야 쓰는 플랫폼이 꺼져 있으면 뺀다)
function usablePlatforms() {
  return platforms.filter((p) => p.enabled !== false);
}

function setBadge(node, on, onText, offText) {
  node.textContent = on ? onText : offText;
  node.className = on ? "badge ok" : "badge";
}

function lines(text) {
  return text.split("\n").map((x) => x.trim()).filter(Boolean);
}

function bytes(n) {
  if (n == null) return "";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

// ---- 화면 등록 ----
// 화면별 파일(views/*.js, 켜진 확장의 파일)이 불러와질 때 자기 화면을 등록한다. 뼈대는 이 표로만 화면을 안다.
//   registerView(이름, { load, tick, bind, leave, enter, hash, fromHash, ownsDisk, viewOnly })
//   load(): 화면을 열 때 읽기 / tick(): 주기 갱신(없으면 갱신하지 않음) / bind(): 시작할 때 한 번(로그인한 화면만)
//   leave(next): 다른 화면으로 가기 전(거짓이면 머문다) / enter(again): 열기 전(거짓이면 다시 읽지 않는다)
//   hash(): 주소의 #뒤 / fromHash(part, viaHashChange): #이름/part로 열 때 / hashChange(part): 그 화면에 있는 채 #이 바뀔 때
//   ownsDisk: 그 화면이 여유 공간을 함께 갱신한다. 보기 전용 모드에서 여는 화면은 VIEW_ONLY_TABS(중심부)뿐이다
const VIEWS = {};
let extensionsInfo = null; // GET /api/extensions(로그인한 화면만)
const BACKGROUND = []; // 화면과 상관없이 주기마다 하는 일 [{ fn, viewOnly }]

function registerView(name, spec) {
  VIEWS[name] = spec;
}

function registerBackground(fn, options = {}) {
  BACKGROUND.push({ fn, viewOnly: Boolean(options.viewOnly) });
}

function show(view) {
  const from = VIEWS[currentView];
  // 설정에 저장하지 않은 변경이 있으면 다른 메뉴로 가기 전에 확인을 받는다
  if (currentView !== view && !viewOnly && from && from.leave && !from.leave(view)) return;
  const again = currentView === view;
  currentView = view;
  for (const tab of document.querySelectorAll(".tab")) tab.setAttribute("aria-selected", String(tab.dataset.view === view));
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== `view-${view}`;
  const spec = VIEWS[view];
  history.replaceState(null, "", (viewOnly ? "?view=1" : location.pathname) + (spec.hash ? spec.hash() : `#${view}`));
  if (spec.enter && spec.enter(again) === false) return; // 고치던 값을 다시 읽어 덮지 않는다
  spec.load();
}

// 주소의 #메뉴 또는 #settings/탭을 읽는다(새로고침·링크로 연 경우)
function viewFromHash() {
  const [view, part] = location.hash.slice(1).split("/");
  if (VIEWS[view] && VIEWS[view].fromHash) VIEWS[view].fromHash(part);
  return view;
}

function tick() {
  if (document.hidden) return;
  const spec = VIEWS[currentView];
  if (spec && spec.tick) spec.tick();
  for (const job of BACKGROUND) if (!viewOnly || job.viewOnly) job.fn(spec);
}

// 색 구성: 자동(브라우저 설정) → 밝게 → 어둡게. 선택은 이 브라우저에만 저장한다
const THEMES = [["auto", "색: 자동"], ["light", "색: 밝게"], ["dark", "색: 어둡게"]];

function currentTheme() {
  const t = document.documentElement.dataset.theme;
  return t === "light" || t === "dark" ? t : "auto";
}

function applyTheme(theme) {
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try {
    if (theme === "auto") localStorage.removeItem("lr-theme"); else localStorage.setItem("lr-theme", theme);
  } catch (e) { /* 저장하지 못해도 이번 화면에는 적용한다 */ }
  showThemeButton(theme);
}

// 글자 대신 그림. 지금 상태는 접근성 이름과 풍선 도움말("색: 어둡게" 등)로 둔다
function showThemeButton(theme) {
  const button = $("theme");
  const label = THEMES.find((x) => x[0] === theme)[1];
  button.dataset.state = theme;
  button.setAttribute("aria-label", label);
  button.title = label;
}

function bindCommon() {
  showThemeButton(currentTheme());
  $("theme").addEventListener("click", () => {
    const i = THEMES.findIndex((x) => x[0] === currentTheme());
    applyTheme(THEMES[(i + 1) % THEMES.length][0]);
  });
  for (const spec of Object.values(VIEWS)) if (spec.bindCommon) spec.bindCommon();
}

const VIEW_ONLY_TABS = ["channels", "history"];

async function bootViewOnly(guide) {
  // 보기 전용: 채널 카드와 기록만 보이고 조작·다른 탭·로그아웃은 숨긴다. 서버도 같은 경로를 거부한다
  viewOnly = true;
  const tabs = VIEW_ONLY_TABS.filter((name) => VIEWS[name]);
  document.body.classList.add("view-only");
  $("view-only-text").textContent = guide;
  $("view-only-banner").hidden = false;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.hidden = !tabs.includes(tab.dataset.view);
    if (!tab.hidden) tab.addEventListener("click", () => show(tab.dataset.view));
  }
  $("logout").hidden = true;
  $("who").textContent = "보기 전용";
  bindCommon();
  const first = location.hash.slice(1);
  show(tabs.includes(first) ? first : "channels");
  timer = setInterval(async () => {
    if (document.hidden) return;
    const m = await fetch("/api/mode", { credentials: "same-origin" }).then((r) => r.json()).catch(() => null);
    if (m && m.mode !== "view") { location.replace("/login"); return; } // 계정이 생기면 로그인으로
    tick();
  }, REFRESH_MS);
}

function applyPasswordRule(rule) {
  // 비밀번호 규칙의 숫자는 서버 상수 하나에서 받는다
  if (!rule) return;
  for (const node of document.querySelectorAll("[data-pw-rule]")) node.textContent = rule.text;
  for (const input of document.querySelectorAll("[data-pw-input]")) { input.minLength = rule.min; input.maxLength = rule.max; }
}

// 켜진 확장이 등록한 위쪽 탭과 설정 묶음의 자리를 만든다(내용은 그 확장의 스크립트가 채운다)
function addExtensionPlaces(ui) {
  if (!ui || !((ui.tabs || []).length || (ui.groups || []).length)) return;
  const nav = document.querySelector(".tabs");
  const main = document.querySelector("main");
  for (const tab of (ui && ui.tabs) || []) {
    if (!VIEWS[tab.view] || $(`view-${tab.view}`)) continue;
    nav.append(el("button", { class: "tab", role: "tab", text: tab.label, dataset: { view: tab.view, ext: tab.ext } }));
    main.append(el("section", { id: `view-${tab.view}`, class: "view", hidden: true, dataset: { ext: tab.ext } }));
  }
  for (const group of (ui && ui.groups) || []) {
    const panel = $(`spanel-${group.subtab}`);
    if (!panel || $(`ext-group-${group.id}`)) continue;
    panel.append(el("section", { id: `ext-group-${group.id}`, class: "panel", dataset: { ext: group.ext } },
      el("h2", { text: group.title })));
  }
}

async function boot() {
  const mode = await fetch("/api/mode", { credentials: "same-origin" }).then((r) => r.json());
  setDisplay(mode.display); // 첫 화면을 그리기 전에 화면 모양을 받는다
  if (mode.mode === "view") { await bootViewOnly(mode.guide); return; }
  applyPasswordRule(mode.password);
  const session = await api("GET", "/api/session");
  csrf = session.csrf;
  $("who").textContent = session.username;
  const info = await api("GET", "/api/platforms");
  platforms = Array.isArray(info.platforms) ? info.platforms : [];
  defaultPlatform = info.default || (platforms[0] && platforms[0].id) || null;
  // 확장 목록(설정 > 일반의 "확장" 묶음)과 켜진 확장의 탭·설정 묶음 자리
  extensionsInfo = await api("GET", "/api/extensions");
  addExtensionPlaces(extensionsInfo && extensionsInfo.ui);
  for (const spec of Object.values(VIEWS)) if (spec.bind) spec.bind(); // 화면마다 단추·폼을 묶는다(등록 순서)

  for (const tab of document.querySelectorAll(".tab")) tab.addEventListener("click", () => show(tab.dataset.view));
  $("logout").addEventListener("click", async () => { await act(() => api("POST", "/api/logout")); location.replace("/login"); });
  bindCommon();

  const first = viewFromHash();
  show(VIEWS[first] ? first : "channels");
  // 주소창에서 #settings/알림처럼 바꾸면 그 화면(과 그 안의 탭)으로 간다
  window.addEventListener("hashchange", () => {
    const [view, part] = location.hash.slice(1).split("/");
    const spec = VIEWS[view];
    if (!spec) return;
    if (view === currentView) {
      if (spec.hashChange) spec.hashChange(part);
    } else {
      if (spec.fromHash) spec.fromHash(part, true);
      show(view);
      if (currentView !== view) {
        const cur = VIEWS[currentView];
        history.replaceState(null, "", location.pathname + (cur.hash ? cur.hash() : `#${currentView}`));
      }
    }
  });
  timer = setInterval(tick, REFRESH_MS);
}

// ---- 화면별 파일 불러오기 ----
// 서버가 index.html의 meta(lr-scripts·lr-styles)에 적어 준 순서대로 불러온다. 하나라도 받지 못하거나 도중에 오류가 나면
// boot.js가 시작 실패 안내를 보인다. 번들 도구는 쓰지 않는다
function metaList(id) {
  const node = $(id);
  return String((node && node.getAttribute("content")) || "").split(/\s+/).filter(Boolean);
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const node = document.createElement("script");
    node.src = src;
    node.async = false;
    node.onload = () => resolve();
    node.onerror = () => reject(new Error(`화면 스크립트를 받지 못했습니다: ${src}`));
    document.head.appendChild(node);
  });
}

async function start() {
  for (const href of metaList("lr-styles")) document.head.appendChild(el("link", { rel: "stylesheet", href }));
  // 불러온 파일이 맨 위에서 오류를 내면(이전 빌드의 파일 등) 거기서 멈춘다. 안내는 boot.js가 이미 띄웠다
  let loadError = null;
  const onError = (event) => { loadError = loadError || event.error || new Error(event.message); };
  window.addEventListener("error", onError);
  try {
    for (const src of metaList("lr-scripts")) {
      await loadScript(src);
      if (loadError) throw loadError;
    }
  } finally {
    window.removeEventListener("error", onError);
  }
  await boot();
}

// 시작이 실패하면 boot.js가 본문 자리에 고정 안내를 보인다. 오류 내용은 콘솔에만 남긴다
if (window.lrBoot) window.lrBoot.run(start);
else start().catch((e) => console.error("화면을 시작하지 못했다", e));
