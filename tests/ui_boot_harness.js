"use strict";
// 시험용. 화면 HTML의 태그를 간단한 가짜 DOM으로 만들고 화면 스크립트를 Node의 vm에서 돌린다.
// 브라우저를 대신하지 않는다. 시작 코드가 실패했을 때 안내가 보이는지, 성공하면 숨은 채인지만 본다.
//   node ui_boot_harness.js '<json>'
//   json: {html, scripts: [...], remove: [id...], responses: {"/api/mode": {status, body} | "reject"}, hash, skip: [file...], clickRetry}
// 화면 뼈대가 만들어 붙이는 <script src="/static/...">도 화면 폴더의 파일을 읽어 차례로 돌린다(skip에 있으면 받지 못한 것으로 친다)
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const cfg = JSON.parse(process.argv[2]);
const removed = new Set(cfg.remove || []);
const consoleErrors = [];
const replaced = [];
let reloads = 0;
const fetched = [];

function makeElement(tag, attrs) {
  const listeners = {};
  const classes = new Set(String(attrs.class || "").split(/\s+/).filter(Boolean));
  const base = {
    tagName: tag.toUpperCase(),
    attrs,
    id: attrs.id || "",
    hidden: Object.prototype.hasOwnProperty.call(attrs, "hidden"),
    textContent: "",
    children: [],
    dataset: {},
    value: "",
    focused: false,
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); for (const c of String(v).split(/\s+/).filter(Boolean)) classes.add(c); },
    classList: {
      add: (...c) => c.forEach((x) => classes.add(x)), remove: (...c) => c.forEach((x) => classes.delete(x)),
      toggle: (c, on) => { const want = on === undefined ? !classes.has(c) : on; if (want) classes.add(c); else classes.delete(c); return want; },
      contains: (c) => classes.has(c),
    },
    hasClass: (c) => classes.has(c),
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter((f) => f !== fn); },
    dispatch(type, event) { for (const fn of listeners[type] || []) fn(event || { type, preventDefault() {} }); },
    listenerCount: (type) => (listeners[type] || []).length,
    append(...nodes) { base.children.push(...nodes); nodes.forEach((n) => onAppend(n)); },
    appendChild(node) { base.children.push(node); onAppend(node); return node; },
    replaceChildren(...nodes) { base.children = nodes; },
    setAttribute(k, v) { attrs[k] = String(v); if (k === "class") base.className = v; },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    removeAttribute(k) { delete attrs[k]; },
    querySelector: () => null,
    querySelectorAll: () => [],
    focus() { base.focused = true; },
    close() {}, showModal() {}, reset() {}, click() { base.dispatch("click"); },
  };
  for (const [k, v] of Object.entries(attrs)) if (k.startsWith("data-")) {
    base.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
  }
  // 폼의 이름 붙은 칸(form.quality 등)처럼 모르는 속성은 그때 만든 빈 요소로 돌려준다
  return new Proxy(base, {
    get(target, prop) {
      if (prop in target || typeof prop === "symbol" || prop === "then" || prop === "toJSON") return target[prop];
      target[prop] = makeElement("div", {});
      return target[prop];
    },
  });
}

let onAppend = () => {};
const html = fs.readFileSync(cfg.html, "utf8");
const elements = [];
for (const m of html.matchAll(/<([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[^\s=>]+(?:="[^"]*")?)*)\s*\/?>/g)) {
  const attrs = {};
  for (const a of m[2].matchAll(/([^\s=>]+)(?:="([^"]*)")?/g)) attrs[a[1]] = a[2] === undefined ? "" : a[2];
  if (attrs.id && removed.has(attrs.id)) continue;
  elements.push(makeElement(m[1].toLowerCase(), attrs));
}

function matches(node, selector) {
  if (selector.startsWith(".")) return node.hasClass(selector.slice(1));
  const attr = selector.match(/^\[([^\]=]+)\]$/);
  if (attr) return attr[1] in node.attrs;
  throw new Error(`가짜 DOM이 모르는 선택자: ${selector}`);
}

const docListeners = {};
const documentElement = makeElement("html", {});
const body = elements.find((e) => e.tagName === "BODY") || makeElement("body", {});
const head = elements.find((e) => e.tagName === "HEAD") || makeElement("head", {});
const document = {
  readyState: "interactive",
  hidden: false,
  body,
  head,
  documentElement,
  getElementById: (id) => elements.find((e) => e.id === id) || null,
  querySelectorAll: (sel) => elements.filter((e) => sel.split(",").some((s) => matches(e, s.trim()))),
  querySelector: (sel) => document.querySelectorAll(sel)[0] || null,
  createElement: (tag) => makeElement(tag, {}),
  createElementNS: (ns, tag) => makeElement(tag, {}),
  createTextNode: (text) => ({ textContent: text }),
  addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
  removeEventListener() {},
};

const winListeners = {};
function fire(type, event) { for (const fn of [...(winListeners[type] || [])]) fn(event); }

function respond(path) {
  const key = Object.keys(cfg.responses || {}).find((p) => path === p || path.startsWith(p + "?"));
  const spec = key ? cfg.responses[key] : { status: 200, body: {} };
  if (spec === "reject") return Promise.reject(new TypeError("Failed to fetch /secret/internal/path"));
  const status = spec.status || 200;
  const payload = spec.body === undefined ? {} : spec.body;
  const resp = {
    ok: status >= 200 && status < 300, status,
    json: () => Promise.resolve(JSON.parse(JSON.stringify(payload))),
    text: () => Promise.resolve(JSON.stringify(payload)),
    clone: () => resp,
    headers: { get: () => null },
  };
  return Promise.resolve(resp);
}

const storage = () => {
  const data = new Map();
  return { getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, String(v)), removeItem: (k) => data.delete(k) };
};

const sandbox = {
  document,
  console: {
    log() {}, warn() {}, info() {},
    error: (...args) => consoleErrors.push(args.map((a) => (a && a.stack) || (a && a.message) || String(a)).join(" ")),
  },
  location: {
    pathname: "/", search: "", hash: cfg.hash || "", href: "http://testserver/",
    replace: (url) => replaced.push(url), reload: () => { reloads += 1; }, assign: (url) => replaced.push(url),
  },
  history: { replaceState() {}, pushState() {} },
  fetch: (path) => { fetched.push(String(path)); return respond(String(path)); },
  setTimeout: (fn, ms) => (ms > 1000 ? 0 : setTimeout(fn, ms)), clearTimeout,
  setInterval: () => 0, clearInterval: () => {},
  sessionStorage: storage(), localStorage: storage(),
  confirm: () => false, alert: () => {},
  addEventListener(type, fn) { (winListeners[type] ||= []).push(fn); },
  removeEventListener(type, fn) { winListeners[type] = (winListeners[type] || []).filter((f) => f !== fn); },
  URL, URLSearchParams, Intl, Date, Math, JSON, Promise, Error, TypeError,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

// 거부를 아무도 받지 않으면 브라우저처럼 window의 unhandledrejection으로 넘긴다
process.on("unhandledRejection", (reason) => fire("unhandledrejection", { reason }));

function runFile(file) {
  try {
    new vm.Script(fs.readFileSync(file, "utf8"), { filename: file }).runInContext(sandbox);
  } catch (err) {
    fire("error", { error: err, message: err.message }); // 브라우저는 스크립트 밖으로 나간 예외를 window error로 알린다
  }
}

onAppend = (node) => {
  if (!node || node.tagName !== "SCRIPT" || !node.src) return;
  const file = path.join(path.dirname(cfg.html), String(node.src).replace(/^\/static\//, ""));
  setTimeout(() => {
    if ((cfg.skip || []).includes(file) || !fs.existsSync(file)) { if (node.onerror) node.onerror({}); return; }
    runFile(file);
    if (node.onload) node.onload({});
  }, 0);
};

for (const file of cfg.scripts) {
  if (cfg.skip && cfg.skip.includes(file)) continue; // 받지 못한 스크립트(404)
  try {
    new vm.Script(fs.readFileSync(file, "utf8"), { filename: file }).runInContext(sandbox);
  } catch (err) {
    fire("error", { error: err, message: err.message }); // 브라우저는 스크립트 밖으로 나간 예외를 window error로 알린다
  }
}
document.readyState = "complete";
for (const fn of docListeners.DOMContentLoaded || []) fn({});
fire("load", {});

setTimeout(() => {
  const panel = document.getElementById("boot-failed");
  const retry = document.getElementById("boot-retry");
  let reloadsAfterClick = null;
  if (cfg.clickRetry && retry) { retry.dispatch("click"); reloadsAfterClick = reloads; }
  const out = {
    panelHidden: panel ? panel.hidden : null,
    hideTargets: document.querySelectorAll("[data-boot-hide]").map((e) => ({ id: e.id || e.tagName, hidden: e.hidden })),
    views: document.querySelectorAll(".view").map((e) => ({ id: e.id, hidden: e.hidden })),
    retryFocused: Boolean(retry && retry.focused),
    reloadsAfterClick,
    consoleErrors,
    replaced,
    reloads,
    fetched,
    toast: (document.getElementById("toast") || {}).textContent || "",
    pwRule: (document.querySelector("[data-pw-rule]") || {}).textContent || "",
  };
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}, 200);
