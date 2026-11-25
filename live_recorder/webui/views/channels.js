"use strict";
// 채널 화면. 카드, 채널 추가·수정 창, 주소 인식. 뼈대(app.js)의 공통 함수를 쓰고 registerView로 자기 화면을 등록한다.

const QUALITIES = ["best", "1080p", "720p", "480p", "360p", "144p", "worst"];
let channels = [];
let editing = null; // 고치는 채널 { channel_id, platform }

// 채널은 (플랫폼, 채널 ID)다. 기본 플랫폼 채널의 주소에는 플랫폼을 적지 않고 다른 플랫폼은 ?platform=을 붙인다
function channelPath(ch, tail = "") {
  const query = ch.platform && ch.platform !== defaultPlatform ? `?platform=${encodeURIComponent(ch.platform)}` : "";
  return `/api/channels/${ch.channel_id}${tail}${query}`;
}

function sameChannel(a, b) {
  return a.channel_id === b.channel_id && (a.platform || defaultPlatform) === (b.platform || defaultPlatform);
}


// ---- 채널 ----
// 채널 카드 색: 녹화 중 녹색, 녹화하지 않음 노란색, 오류 붉은색.
// 오류는 오류 표시(연속 실패·디스크 부족 등)가 켜졌거나 실패 뒤 다시 시도를 기다리는(대기) 상태다
function channelHealth(ch) {
  if (ch.error_flag || ch.state === "backoff") return "error";
  if (ch.state === "recording") return "recording";
  return "idle";
}

// 썸네일 위에 올리는 상태 표시. 색만으로 구분하지 않고 글자를 함께 쓴다
function stateText(ch) {
  if (ch.error_flag) return "오류";
  if (ch.state === "backoff") return "대기";
  if (ch.state === "recording") return `녹화 ${hms(ch.elapsed)}`;
  return ch.state_label;
}

const SVG_NS = "http://www.w3.org/2000/svg";
// 상태 아이콘(자체 그림). 방송 중이 아니거나 썸네일을 못 불러오면 이것만 보인다
const ICONS = {
  recording: ["M12 7a5 5 0 1 0 0 10a5 5 0 1 0 0-10z", "M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20zm0 2a8 8 0 1 1 0 16a8 8 0 1 1 0-16z"],
  watching: ["M12 5C6.5 5 2.7 9.4 1.5 12c1.2 2.6 5 7 10.5 7s9.3-4.4 10.5-7C21.3 9.4 17.5 5 12 5zm0 2c3.9 0 6.9 3 8.2 5c-1.3 2-4.3 5-8.2 5s-6.9-3-8.2-5C5.1 10 8.1 7 12 7z", "M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6z"],
  finalizing: ["M12 3a9 9 0 1 0 9 9h-2a7 7 0 1 1-7-7z", "M11 7h2v6h-2z", "M11 11h5v2h-5z"],
  error: ["M12 2L1 21h22zm0 4.2L19.6 19H4.4z", "M11 10h2v5h-2zm0 6h2v2h-2z"],
  stopped: ["M7 5h3v14H7zm7 0h3v14h-3z"],
  adult: ["M12 2a10 10 0 1 0 0 20a10 10 0 1 0 0-20zm0 2a8 8 0 0 1 6.3 12.9L7.1 5.7A8 8 0 0 1 12 4zM5.7 7.1l11.2 11.2A8 8 0 0 1 5.7 7.1z"],
};

function icon(kind) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "icon");
  for (const d of ICONS[kind] || ICONS.stopped) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

function iconKind(ch) {
  if (ch.adult) return "adult";
  const health = channelHealth(ch);
  if (health === "error") return "error";
  if (ch.state === "recording") return "recording";
  if (ch.state === "finalizing") return "finalizing";
  if (ch.state === "watching") return "watching";
  return "stopped";
}

// 썸네일 주소는 상태를 갱신할 때 함께 바꾸되 1분에 한 번 이하다. 같은 그림 요소를 다시 써서 깜빡이지 않게 한다
const THUMB_MIN_MS = 60000;
const thumbs = new Map();

function thumbnail(key, ch) {
  if (!ch.thumb_url || ch.adult) { thumbs.delete(key); return null; }
  const now = Date.now();
  let entry = thumbs.get(key);
  if (!entry) {
    const img = el("img", { class: "thumb-img", alt: "", loading: "lazy", decoding: "async", referrerpolicy: "no-referrer" });
    entry = { img, url: "", at: 0, failed: false };
    img.addEventListener("error", () => { entry.failed = true; img.hidden = true; });
    img.addEventListener("load", () => { entry.failed = false; img.hidden = false; });
    thumbs.set(key, entry);
  }
  if (entry.url !== ch.thumb_url && now - entry.at >= THUMB_MIN_MS) {
    entry.url = ch.thumb_url;
    entry.at = now;
    entry.failed = false;
    entry.img.src = ch.thumb_url;
  }
  return entry.failed ? null : entry.img;
}

function cardSubline(ch) {
  // 녹화 중: 받은 용량과 파일의 시작 시각. 감시 등: 마지막 녹화의 날짜와 길이. 오류: 사유와 연속 실패 횟수
  const health = channelHealth(ch);
  if (health === "error") {
    const reason = viewOnly ? (ch.error_reason || "녹화 실패") : (ch.error || "녹화 실패");
    return ch.error_count ? `연속 실패 ${ch.error_count}회 · ${reason}` : reason; // 횟수를 앞에 둬 줄임에 가리지 않게
  }
  if (ch.state === "recording") {
    const parts = [];
    if (ch.bytes) parts.push(bytes(ch.bytes));
    if (ch.file_started_at) parts.push(`${formatWhen(ch.file_started_at)}부터`);
    return parts.join(" · ") || "받기 시작";
  }
  const prefix = ch.state === "finalizing" ? "마무리 중 · " : "";
  if (ch.last_started_at) return `${prefix}마지막 녹화 ${formatWhen(ch.last_started_at, { time: false })} · ${hms(ch.last_seconds)}`;
  return `${prefix}녹화 기록 없음`;
}

function channelCard(ch, index) {
  const recording = ch.state === "recording";
  const health = channelHealth(ch);
  const key = ch.channel_id || `#${index}`;
  const img = thumbnail(key, ch);
  const thumb = el("div", { class: "thumb" }, img || el("div", { class: "thumb-icon" }, icon(iconKind(ch))),
    el("span", { class: `overlay state ${health}`, text: stateText(ch) }),
    recording && ch.quality_label ? el("span", { class: "overlay quality", text: ch.quality_label }) : null,
    ch.adult ? el("span", { class: "overlay adult", text: "연령 제한" }) : null,
  );
  const body = el("div", { class: "card-body" },
    el("div", { class: "card-name" }, el("strong", { text: ch.name }),
      // 플랫폼이 둘 이상 들어 있으면 줄마다 짧은 표지
      platforms.length > 1 && ch.platform_name ? el("span", { class: "badge platform-badge", text: ch.platform_name }) : null),
    // 꺼진 플랫폼의 채널은 남아 있고 감시만 멈춘다
    ch.platform_off ? el("div", { class: "card-sub platform-off", text: ch.platform_note || "플랫폼이 꺼져 있습니다" }) : null,
    el("div", { class: "card-title", title: ch.live_title || "", text: ch.live_title || (recording ? "제목 없음" : "방송 중이 아님") }),
    el("div", { class: "card-sub", title: cardSubline(ch), text: cardSubline(ch) }),
  );
  if (ch.file) body.append(el("div", { class: "card-file", title: ch.file, text: ch.file }));
  const dataset = { state: ch.state, health };
  if (viewOnly) return el("article", { class: "card", dataset }, thumb, body);

  const auto = el("input", { type: "checkbox", "aria-label": "자동 녹화" });
  auto.checked = ch.auto_record;
  auto.addEventListener("change", async () => {
    const ok = await act(() => api("PATCH", channelPath(ch), { auto_record: auto.checked }),
      auto.checked ? "자동 녹화를 켰습니다" : "자동 녹화를 껐습니다");
    if (!ok) auto.checked = !auto.checked;
    loadChannels();
  });
  const primary = recording || ch.state === "backoff"
    ? el("button", { class: "btn danger", text: "중지", dataset: { action: "stop" },
      onclick: () => act(() => api("POST", channelPath(ch, "/stop")), (r) => r.result).then(loadChannels) })
    : el("button", { class: "btn primary", text: "지금 녹화", dataset: { action: "start" },
      onclick: () => act(() => api("POST", channelPath(ch, "/start")), (r) => r.result).then(loadChannels) });
  body.append(el("div", { class: "card-controls" },
    el("label", { class: "switch" }, auto, "자동"),
    el("span", { class: "grow" }),
    primary,
    el("button", { class: "btn small", text: "수정", dataset: { action: "edit" }, onclick: () => openChannelDialog(ch) }),
  ));
  dataset.channel = ch.channel_id;
  return el("article", { class: "card", dataset }, thumb, body);
}

// 채널에 플랫폼이 둘 이상 섞여 있을 때만 플랫폼 거르기 단추를 보인다. 위쪽 탭은 늘리지 않는다
let channelFilter = "";

function showPlatformFilter() {
  const box = $("channel-filter");
  // 꺼진 플랫폼은 거르기 단추에서 감춘다
  const used = [...new Set(channels.map((ch) => ch.platform).filter((id) => id && usablePlatforms().some((p) => p.id === id)))];
  if (viewOnly || used.length < 2) {
    channelFilter = "";
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  if (channelFilter && !used.includes(channelFilter)) channelFilter = "";
  const button = (value, text) => el("button", {
    class: "btn small", type: "button", text, "aria-pressed": String(channelFilter === value),
    onclick: () => { channelFilter = value; loadChannels(); },
  });
  box.replaceChildren(button("", "전체"), ...used.map((id) => button(id, (platformOf(id) || { name: id }).name)));
  box.hidden = false;
}

// 채널이 많아 한 바퀴가 늘어났거나 플랫폼 창구가 멈췄으면 알린다
function showGateNote(gates) {
  const box = $("gate-note");
  const lines = Object.entries(gates || {}).map(([pid, g]) => {
    const name = (platformOf(pid) || { name: pid }).name;
    if (g.paused) return `${name}: 플랫폼이 확인을 막아 확인을 멈췄습니다(약 ${Math.ceil(g.pause_left / 60)}분 남음)`;
    return `${name}: ${g.note}`;
  });
  box.textContent = lines.join(" / ");
  box.hidden = !lines.length;
}

function addCard() {
  // 마지막 칸은 채널 추가 카드다
  return el("button", { class: "card add-card", id: "add-channel", type: "button", onclick: () => openChannelDialog(null) },
    el("span", { class: "plus", text: "+" }), el("span", { text: "채널 추가" }));
}

function showDisk(disk) {
  // 상단 오른쪽에 녹화 디스크의 여유 공간을 늘 보인다
  const box = $("disk");
  if (!disk) { box.textContent = "여유 공간 ?"; return; }
  box.textContent = `여유 ${bytes(disk.free)}`;
  box.title = `녹화 디스크 여유 ${bytes(disk.free)} / 전체 ${bytes(disk.total)}`;
  box.classList.toggle("low", disk.total > 0 && disk.free / disk.total < 0.05);
}

async function fetchChannels() {
  const data = await api("GET", viewOnly ? "/api/view/channels" : "/api/channels");
  channels = data.channels;
  showDisk(data.disk);
  showGateNote(data.gates);
  return data;
}

async function loadChannels() {
  try { await fetchChannels(); } catch (e) { $("refreshed").textContent = "갱신 실패: " + e.message; return; }
  if (currentView !== "channels") return;
  const list = $("channel-list");
  // 포커스가 카드 안의 입력칸에 있으면 다시 그리지 않는다(누르던 스위치가 사라지지 않게)
  if (list.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
  showPlatformFilter();
  const shown = channels.filter((ch) => !channelFilter || ch.platform === channelFilter);
  const cards = shown.map((ch) => channelCard(ch, channels.indexOf(ch) + 1));
  if (!viewOnly) cards.push(addCard());
  if (!cards.length) cards.push(el("div", { class: "empty", text: "등록한 채널이 없습니다." }));
  list.replaceChildren(...cards);
  $("refreshed").textContent = "갱신 " + formatWhen(Date.now(), { date: false, seconds: true });
}

// 채널 주소나 ID에서 채널 ID를 뽑는다. 서버는 지금처럼 ID만 받는다(채널 ID 자리에 URL을 받지 않는다).
// 규칙은 플랫폼이 준다(channel_rule: id_pattern, hosts, path_prefix, error). 받는 모양: ID 그대로,
// https://<호스트>/<path_prefix>/<ID>, https://<호스트>/<ID>(채널 화면). 앞의 https://는 없어도 되고
// 뒤의 경로·쿼리·#는 무시한다. 그 밖의 호스트와 모양은 거부한다.
function channelRule(platformId) {
  const p = platformOf(platformId);
  return p ? p.channel_rule : null;
}

function parseChannelId(text, rule = channelRule()) {
  if (!rule) return null;
  // 대소문자를 가리는 ID(예: 유튜브 UC…)는 그대로 둔다
  const keep = Boolean(rule.case_sensitive);
  const idRe = new RegExp(rule.id_pattern, keep ? "" : "i");
  const norm = (v) => (keep ? v : v.toLowerCase());
  const value = String(text || "").trim();
  if (idRe.test(value)) return norm(value);
  let url;
  try {
    url = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(value) ? value : `https://${value}`);
  } catch (e) {
    return null;
  }
  if (!["https:", "http:"].includes(url.protocol)) return null;
  if (!rule.hosts.includes(url.hostname.toLowerCase())) return null;
  const parts = url.pathname.split("/").filter(Boolean);
  const id = parts[0] === rule.path_prefix ? parts[1] : parts[0];
  return id && idRe.test(id) ? norm(id) : null;
}

function formPlatform() {
  const form = $("channel-form");
  return editing ? editing.platform : (form.platform.value || defaultPlatform);
}

// 조회가 필요한 이름(`@이름`). 플랫폼 규칙의 lookup_pattern으로 보고, 그 플랫폼 주소의 첫 경로(`/@이름`)도 받는다
function channelLookup(text, rule) {
  if (!rule || !rule.lookup_pattern) return null;
  const re = new RegExp(rule.lookup_pattern, "u");
  const value = String(text || "").trim();
  if (re.test(value)) return value;
  let url;
  try {
    url = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(value) ? value : `https://${value}`);
  } catch (e) {
    return null;
  }
  if (!["https:", "http:"].includes(url.protocol) || !rule.hosts.includes(url.hostname.toLowerCase())) return null;
  let first = url.pathname.split("/").filter(Boolean)[0] || "";
  try { first = decodeURIComponent(first); } catch (e) { return null; }
  return re.test(first) ? first : null;
}

// 채널 칸의 글: { id } 또는 { lookup }(저장할 때 서버가 조회), 못 읽으면 null
function readChannelText(text, platformId) {
  const rule = channelRule(platformId);
  const id = parseChannelId(text, rule);
  if (id) return { id };
  const lookup = channelLookup(text, rule);
  return lookup ? { lookup } : null;
}

// 붙여 넣은 주소로 플랫폼을 알아서 고른다. 한 플랫폼만 읽으면 그 플랫폼, 여럿이면 ""(고르게 한다), 없으면 null
function detectPlatform(text) {
  const value = String(text || "").trim();
  if (!value) return null;
  const hits = platforms.filter((p) => readChannelText(value, p.id));
  if (hits.length === 1) return hits[0].id;
  return hits.length ? "" : null;
}

function showChannelIdNote() {
  const form = $("channel-form");
  const note = $("channel-id-note");
  const raw = form.channel_id.value.trim();
  if (!raw || form.channel_id.disabled) { note.hidden = true; return null; }
  const found = editing ? null : detectPlatform(raw);
  const off = found && platforms.find((p) => p.id === found && p.enabled === false);
  if (off) {
    // 꺼진 플랫폼의 주소: 추가하지 않고 먼저 켜라고 안내한다
    note.textContent = (off.notice && off.notice.need_enable) || "이 플랫폼을 먼저 켜 주십시오";
    note.className = "form-error field-note";
    note.hidden = false;
    return null;
  }
  if (found && found !== form.platform.value) {
    form.platform.value = found;
    setChannelHint();
  }
  const rule = channelRule(formPlatform());
  const parsed = readChannelText(raw, formPlatform());
  if (parsed && parsed.id) note.textContent = `인식한 채널 ID: ${parsed.id}`;
  else if (parsed) note.textContent = `${parsed.lookup}: 저장할 때 채널을 조회해 ID와 이름을 채웁니다`;
  else if (found === "") note.textContent = "여러 플랫폼이 읽을 수 있는 글입니다. 플랫폼을 고르십시오";
  else note.textContent = rule ? rule.error : "플랫폼 정보를 받지 못했습니다";
  note.className = parsed ? "muted field-note" : "form-error field-note";
  note.hidden = false;
  return parsed;
}

function fillPlatformField() {
  const form = $("channel-form");
  form.platform.replaceChildren(...usablePlatforms().map((p) => el("option", { value: p.id, text: p.name })));
  form.platform.value = defaultPlatform || "";
  $("channel-platform-field").hidden = usablePlatforms().length < 2; // 플랫폼이 하나면 지금과 같게 보인다
  setChannelHint();
}

function setChannelHint() {
  const p = platformOf(formPlatform());
  const form = $("channel-form");
  form.channel_id.placeholder = p ? p.channel_hint : "";
  showFolderHint();
}

// 비워 두면 `<플랫폼 식별자>/<채널 이름>`에 저장한다. 이름 칸이 비어 있으면(조회로 채워질 이름) 그 자리를 글로 보인다.
// 이름의 못 쓰는 글자는 서버가 `_`로 바꾸고, 같은 폴더를 쓰는 채널이 있으면 끝에 채널 ID 앞 8자를 붙인다(안내 글은 어림이다)
function showFolderHint() {
  const form = $("channel-form");
  if (editing) { form.output_dir.placeholder = "비워 두면 바꾸지 않습니다"; return; }
  const p = platformOf(formPlatform());
  const name = form.name.value.trim().replace(/[\\/*?:"<>|+]/g, "_").replace(/\s+/g, " ").replace(/^[ ._]+|[ ._]+$/g, "");
  form.output_dir.placeholder = p ? `비워 두면 ${p.id}/${name || "(채널 이름)"}에 저장합니다` : "";
}

function openChannelDialog(ch) {
  const form = $("channel-form");
  editing = ch ? { channel_id: ch.channel_id, platform: ch.platform } : null;
  $("channel-dialog-title").textContent = ch ? "채널 수정" : "채널 추가";
  $("channel-error").hidden = true;
  form.reset();
  form.platform.value = ch ? ch.platform : defaultPlatform || "";
  form.platform.disabled = Boolean(ch); // 플랫폼도 채널 ID처럼 바꿀 수 없다
  setChannelHint();
  form.channel_id.value = ch ? ch.channel_id : "";
  form.channel_id.disabled = Boolean(ch); // 채널 ID는 수정할 수 없다
  $("channel-id-note").hidden = true;
  form.name.value = ch ? ch.name : "";
  form.output_dir.value = ch ? ch.output_dir : "";
  showFolderHint();
  form.quality.value = ch ? ch.quality : "best";
  form.extension.value = ch ? ch.extension : ".mp4";
  form.video_codec.value = ch && ch.video_codec ? ch.video_codec : "h264";
  form.auto_record.checked = ch ? ch.auto_record : true;
  $("channel-delete").hidden = !ch; // 삭제는 수정 창 안에 둔다(카드에는 주 버튼과 수정만)
  $("channel-dialog").showModal();
}

async function submitChannel(event) {
  event.preventDefault();
  const form = $("channel-form");
  const fields = {
    name: form.name.value, output_dir: form.output_dir.value, quality: form.quality.value,
    extension: form.extension.value, auto_record: form.auto_record.checked, video_codec: form.video_codec.value,
  };
  try {
    if (editing) {
      // 바뀐 값만 보낸다
      const before = channels.find((c) => sameChannel(c, editing)) || {};
      const changes = {};
      for (const [key, value] of Object.entries(fields)) if (before[key] !== value) changes[key] = value;
      if (Object.keys(changes).length) await api("PATCH", channelPath(editing), changes);
    } else {
      const platform = formPlatform();
      const parsed = readChannelText(form.channel_id.value, platform);
      const offFound = detectPlatform(form.channel_id.value);
      const off = offFound && platforms.find((p) => p.id === offFound && p.enabled === false);
      if (off) throw new Error((off.notice && off.notice.need_enable) || "이 플랫폼을 먼저 켜 주십시오");
      if (!parsed) {
        showChannelIdNote();
        throw new Error(platform === defaultPlatform ? "채널 주소나 32자리 채널 ID를 넣으십시오" : "채널 주소나 채널 ID를 넣으십시오");
      }
      let channelId = parsed.id;
      if (parsed.lookup) {
        // `@이름`은 한 번 조회해 채널 ID와 이름으로 바꾼다. 찾지 못하면 까닭을 보이고 추가하지 않는다
        const found = await api("POST", "/api/channels/resolve", { text: parsed.lookup, platform });
        channelId = found.channel_id;
        if (!fields.name.trim() && found.name) { form.name.value = fields.name = found.name; showFolderHint(); }
      }
      if (!fields.name.trim()) throw new Error("표시 이름을 넣으십시오");
      form.channel_id.value = channelId; // 주소를 넣었으면 뽑은 ID로 바꿔 보여 준다
      // 조회로만 추가하는 플랫폼은 `@이름`을 그대로 보낸다(서버가 조회하며 핸들을 함께 기록한다)
      const rule = channelRule(platform) || {};
      const body = { channel_id: parsed.lookup && rule.lookup_only ? parsed.lookup : channelId, ...fields };
      if (platform !== defaultPlatform) body.platform = platform; // 기본 플랫폼이면 플랫폼을 적지 않는다
      await api("POST", "/api/channels", body);
    }
  } catch (e) {
    $("channel-error").textContent = e.message;
    $("channel-error").hidden = false;
    return;
  }
  $("channel-dialog").close();
  toast(editing ? "채널을 수정했습니다" : "채널을 추가했습니다");
  loadChannels();
}

async function removeChannel(ch) {
  const note = ch.state === "recording" ? "\n진행 중인 녹화는 중지하고 마무리합니다." : "";
  if (!confirm(`'${ch.name}' 채널을 지울까요? 녹화한 파일은 남습니다.${note}`)) return;
  const ok = await act(() => api("DELETE", channelPath(ch)), "채널을 지웠습니다");
  if (ok) $("channel-dialog").close();
  loadChannels();
}

function bindChannels() {
  for (const q of QUALITIES) $("channel-form").quality.append(el("option", { text: q }));
  fillPlatformField();
  $("channel-cancel").addEventListener("click", () => $("channel-dialog").close());
  $("channel-delete").addEventListener("click", () => {
    const ch = editing && channels.find((c) => sameChannel(c, editing));
    if (ch) removeChannel(ch);
  });
  $("channel-form").addEventListener("submit", submitChannel);
  $("channel-form").channel_id.addEventListener("input", showChannelIdNote);
  $("channel-form").name.addEventListener("input", showFolderHint);
  $("channel-form").platform.addEventListener("change", () => { setChannelHint(); showChannelIdNote(); });
}

registerView("channels", { load: loadChannels, tick: loadChannels, bind: bindChannels, ownsDisk: true });
// 다른 화면에서도 여유 공간을 갱신한다
registerBackground((spec) => { if (!spec || !spec.ownsDisk) fetchChannels().catch(() => null); }, { viewOnly: true });
