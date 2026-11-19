"use strict";
// downloads 확장의 화면: 다시보기 탭의 "채널 목록" 방식. 플랫폼을 모른다.
// 목록 공급자(확장이 등록한다. 치지직은 replay, 씨미는 cime_media)가 서버에 있을 때만 보인다. 받기는 /api/replay/select로 한다.

// ---- 채널 다시보기 목록 ----
// 화면은 서버가 수집해 둔 목록만 읽는다.
const VL_MAX = 30;
const VL_STATES = {
  downloaded: ["받음", "ok"], missing: ["받았으나 파일 없음", "warn"], queued: ["대기", ""], running: ["받는 중", "warn"], failed: ["실패", "bad"],
};
const VL_NO_MONTH = "none"; // 게시 날짜가 빠진 줄
const VL_SECONDS_PER_PAGE = 4; // 전체 다시 수집에 걸리는 시간의 어림값
const vl = { channel: "", platform: "", providers: [], chans: new Map(), months: [], month: null, videos: [], collect: null,
  selected: new Map(), searchTimer: null };

// 채널은 (플랫폼, 채널 ID)다. 고르는 칸의 값은 둘을 이은 것이다
function vlKey(platform, id) {
  return `${platform}/${id}`;
}

function vlProvider(platform) {
  return vl.providers.find((p) => p.platform === (platform || vl.platform)) || vl.providers[0] || null;
}

// 채널 경로(끝에 `?`를 붙여 부른다)와 질의(플랫폼과 그 밖의 값)
function vlPath(suffix = "?") {
  return `/api/replay/channels/${encodeURIComponent(vl.channel)}${suffix}`;
}

function vlParams(extra = {}) {
  return new URLSearchParams({ platform: vl.platform, ...extra }).toString();
}

function replayMode(mode) {
  if (mode === "list" && !vl.providers.length) mode = "url"; // 공급자가 없으면 목록 보기는 없다
  $("replay-mode-list").setAttribute("aria-selected", String(mode === "list"));
  $("replay-mode-url").setAttribute("aria-selected", String(mode === "url"));
  $("replay-list-pane").hidden = mode !== "list";
  $("replay-url-pane").hidden = mode !== "url";
}

function vlCollecting() {
  return Boolean(vl.collect && (vl.collect.status === "running" || vl.collect.status === "queued"));
}

function vlEmpty(text) {
  $("vl-body").replaceChildren(el("tr", {}, el("td", { colspan: "7", class: "muted", text })));
}

// 고르는 칸에 플랫폼과 녹화 채널인지 표시한다(플랫폼이 둘 이상일 때 플랫폼 표지)
function channelLabel(c, count) {
  const tag = vl.providers.length > 1 ? `[${c.platform_name}] ` : "";
  return `${tag}${c.name}${c.recording ? " · 녹화 채널" : ""} (${count}개)`;
}

function channelOption(c) {
  return el("option", { value: vlKey(c.platform, c.channel_id), text: channelLabel(c, c.collect ? c.collect.count : 0) });
}

// 공급자 목록으로 화면을 맞춘다: 없으면 목록 보기를 감추고, 추가 칸에 플랫폼을 고르게 한다
function showProviders() {
  const has = vl.providers.length > 0;
  const first = $("replay-modes").hidden && has;
  $("replay-modes").hidden = !has;
  if (!has) { replayMode("url"); return; }
  if (first) replayMode("list"); // 공급자가 있으면 처음에는 채널 목록을 보인다(예전과 같다)
  const pick = $("vl-add-platform");
  const before = pick.value;
  pick.replaceChildren(...vl.providers.map((p) => el("option", { value: p.platform, text: p.name })));
  pick.hidden = vl.providers.length < 2;
  if (vl.providers.some((p) => p.platform === before)) pick.value = before;
  showAddHint();
}

function showAddHint() {
  const p = vlProvider($("vl-add-platform").value);
  $("vl-add-input").placeholder = p ? p.add_hint : "";
}

function showTypeFilter() {
  const p = vlProvider();
  const types = Boolean(p && p.video_types);
  $("vl-type").closest("label").hidden = !types;
  if (!types) $("vl-type").value = "";
}

async function loadVideoChannels() {
  let data;
  try {
    data = await api("GET", "/api/replay/channels");
  } catch (e) {
    if (e.status === 404) { vl.providers = []; showProviders(); return; } // 공급자가 없다(예: 그 플랫폼이 꺼짐)
    toast(e.message, true);
    return;
  }
  const select = $("vl-channel");
  const current = vl.channel;
  vl.providers = data.providers || [];
  showProviders();
  vl.chans = new Map(data.channels.map((c) => [vlKey(c.platform, c.channel_id), c]));
  select.replaceChildren(...data.channels.map(channelOption));
  select.disabled = !data.channels.length;
  $("vl-remove").disabled = !data.channels.length;
  if (!data.channels.length) {
    vl.channel = "";
    vl.collect = null;
    vl.months = [];
    vl.videos = [];
    showCollect();
    renderMonths();
    vlEmpty(vl.providers.length ? "다시보기 채널이 없습니다. 위 칸에 채널 주소를 넣어 추가하십시오." : "");
    showSelected();
    return;
  }
  const before = vlKey(vl.platform, current);
  const chosen = vl.chans.get(before) || data.channels[0];
  vl.channel = chosen.channel_id;
  vl.platform = chosen.platform;
  select.value = vlKey(vl.platform, vl.channel);
  if (vlKey(vl.platform, vl.channel) !== before) vl.month = null;
  showTypeFilter();
  await openVideoChannel();
}

// 채널을 열 때: 처음이면 전체 수집, 마지막 확인에서 30분이 지났으면 새 영상 확인을 서버가 스스로 정한다
async function openVideoChannel() {
  if (!vl.channel) return;
  await act(() => api("POST", vlPath("/collect?") + vlParams(), { mode: "auto" }));
  await refreshVideoList(true);
}

async function refreshVideoList(force) {
  if (!vl.channel) return;
  const type = $("vl-type").value;
  const data = await act(() => api("GET", vlPath("/months?") + vlParams({ type })));
  if (!data) return;
  const before = vl.months.find((m) => m.month === vl.month);
  vl.months = data.months;
  vl.collect = data.collect;
  if (!vl.months.some((m) => m.month === vl.month)) vl.month = vl.months.length ? vl.months[0].month : null;
  const after = vl.months.find((m) => m.month === vl.month);
  showCollect();
  renderMonths();
  // 수집 중에 다시 부를 때는 고른 달의 수가 바뀐 경우에만 줄을 다시 받는다
  if (force || !before || !after || before.count !== after.count) await loadVideos();
}

function collectText(c) {
  if (!c || !c.status) return "아직 수집하지 않았습니다";
  if (c.status === "running") return `수집 중 ${c.pages_done}/${c.total_pages == null ? "?" : c.total_pages}쪽 · ${c.fetched}개`;
  if (c.status === "queued") return `수집 차례를 기다린다 · ${c.count}개`;
  const when = c.checked_at ? ` · 마지막 수집 ${formatWhen(c.checked_at)}` : "";
  return `${c.count}개${when} · ${c.status_label}`;
}

function showCollect() {
  const c = vl.collect;
  const busy = vlCollecting();
  $("vl-collect").textContent = vl.channel ? collectText(c) : "";
  $("vl-latest").hidden = busy || !vl.channel;
  $("vl-full").hidden = busy || !vl.channel;
  $("vl-collect-cancel").hidden = !busy;
  // 실패하거나 취소해도 받은 줄은 남는다. 이유와 받은 쪽수를 보인다
  const failed = c && ["failed", "cancelled", "interrupted"].includes(c.status);
  const note = $("vl-collect-error");
  note.hidden = !failed;
  note.textContent = failed ? `${c.error || c.status_label} · ${c.pages_done}/${c.total_pages == null ? "?" : c.total_pages}쪽 받음` : "";
  const key = vlKey(vl.platform, vl.channel);
  const option = [...$("vl-channel").options].find((o) => o.value === key);
  const info = vl.chans.get(key);
  if (option && c && info) option.textContent = channelLabel(info, c.count);
}

function monthLabel(month) {
  return month === VL_NO_MONTH ? "날짜 없음" : `${month.slice(0, 4)}년 ${Number(month.slice(5, 7))}월`;
}

function searching() {
  return Boolean($("vl-search").value.trim());
}

// 영상이 있는 달만 년도별로 묶어 단추로 늘어놓는다. 이전 달·다음 달 단추도 둔다
function renderMonths() {
  const query = searching();
  $("vl-months").hidden = query || !vl.months.length;
  const index = vl.months.findIndex((m) => m.month === vl.month);
  const current = vl.months[index];
  $("vl-month-label").textContent = current ? `${monthLabel(current.month)} · ${current.count}개` : "";
  $("vl-month-prev").disabled = index < 0 || index >= vl.months.length - 1; // 이전 달 = 더 오래된 달
  $("vl-month-next").disabled = index <= 0;
  const years = new Map();
  for (const entry of vl.months) {
    const year = entry.month === VL_NO_MONTH ? "날짜 없음" : `${entry.month.slice(0, 4)}년`;
    if (!years.has(year)) years.set(year, []);
    years.get(year).push(entry);
  }
  const groups = [];
  for (const [year, entries] of years) {
    const group = el("div", { class: "month-year" }, el("span", { text: year }));
    for (const entry of [...entries].reverse()) {
      const text = entry.month === VL_NO_MONTH ? "날짜 없음" : `${Number(entry.month.slice(5, 7))}월`;
      group.append(el("button", {
        class: "month-chip", type: "button", dataset: { month: entry.month }, "aria-current": String(entry.month === vl.month),
        "aria-label": `${monthLabel(entry.month)} ${entry.count}개`, onclick: () => selectMonth(entry.month),
      }, text, el("small", { text: String(entry.count) })));
    }
    groups.push(group);
  }
  $("vl-month-chips").replaceChildren(...groups);
}

async function selectMonth(month) {
  if (!month || month === vl.month) return;
  vl.month = month;
  renderMonths();
  await loadVideos();
}

function stepMonth(delta) {
  const index = vl.months.findIndex((m) => m.month === vl.month);
  const next = vl.months[index + delta];
  if (next) selectMonth(next.month);
}

async function loadVideos() {
  if (!vl.channel) return;
  const query = $("vl-search").value.trim();
  const note = $("vl-search-note");
  renderMonths();
  if (query) {
    const data = await act(() => api("GET", vlPath("/videos?") + vlParams({ q: query, type: $("vl-type").value })));
    if (!data) return;
    vl.videos = data.videos;
    note.textContent = data.truncated ? `검색 결과가 많아 최근 ${data.limit}개만 보입니다. 검색어를 좁히십시오` : `검색 결과 ${data.videos.length}개 · 수집한 목록 전체에서 찾았습니다`;
    note.hidden = false;
  } else {
    note.hidden = true;
    if (!vl.month) {
      vl.videos = [];
    } else {
      const data = await act(() => api("GET", vlPath("/videos?") + vlParams({ month: vl.month, type: $("vl-type").value })));
      if (!data) return;
      vl.videos = data.videos;
    }
  }
  renderVideos();
}

function videoDate(v) {
  return v.publish_date || "";
}

function renderVideos() {
  const query = searching();
  const rows = [];
  let month = null;
  for (const v of vl.videos) {
    // 검색 결과는 달 머리글로 나눈다
    const m = v.month || VL_NO_MONTH;
    if (query && m !== month) {
      month = m;
      rows.push(el("tr", { class: "month-row" }, el("td", { colspan: "7", text: monthLabel(m) })));
    }
    rows.push(videoRow(v));
  }
  const empty = vlCollecting() ? "목록을 수집하는 중입니다. 받은 달부터 보입니다." : (query ? "찾은 영상이 없습니다." : "영상이 없습니다.");
  if (rows.length) $("vl-body").replaceChildren(...rows); else vlEmpty(empty);
  showSelected();
}

// 달을 옮기거나 검색해도 고른 줄은 남고, 고른 수를 늘 보인다
function showSelected() {
  $("vl-selected").textContent = `${vl.selected.size}개 선택 (최대 ${VL_MAX}개)`;
  $("vl-download").disabled = !vl.selected.size;
  $("vl-clear").disabled = !vl.selected.size;
  $("vl-all").checked = vl.videos.length > 0 && vl.videos.every((v) => vl.selected.has(vlPick(v)));
}

function videoRow(v) {
  const pick = el("input", { type: "checkbox", "aria-label": "고르기" });
  pick.checked = vl.selected.has(vlPick(v));
  pick.addEventListener("change", () => { toggleVideo(v, pick.checked); renderVideos(); });
  const thumb = v.thumbnail ? el("img", { class: "vl-thumb", src: v.thumbnail, alt: "", loading: "lazy", referrerpolicy: "no-referrer" })
    : el("div", { class: "vl-thumb-empty", text: v.adult ? "성인" : "" });
  const state = VL_STATES[v.state];
  return el("tr", { dataset: { video: v.video_no } },
    el("td", { class: "check-col" }, pick),
    el("td", {}, el("div", { class: "vl-video" }, thumb, el("div", {}, el("div", { text: v.title || `영상 ${v.video_no}` }),
      v.adult ? el("span", { class: "badge bad", text: "성인" }) : null))),
    el("td", { class: "num", text: formatWhen(videoDate(v), { time: false }) }),
    el("td", { class: "num", text: v.duration ? hms(v.duration) : "" }),
    el("td", { text: v.video_type === "replay" ? "다시보기" : "일반 업로드" }),
    el("td", {}, state ? el("span", { class: `badge ${state[1]}`, text: state[0] }) : null),
    el("td", {}, el("button", { class: "btn small", type: "button", text: "옵션", onclick: () => videoOptions(v) })));
}

// 고른 줄은 채널을 옮겨도 남는다. 열쇠는 플랫폼과 영상 번호, 값은 받을 때 보낼 것
function vlPick(v) {
  return `${vl.platform}/${v.video_no}`;
}

function toggleVideo(v, on) {
  const key = vlPick(v);
  if (on && !vl.selected.has(key) && vl.selected.size >= VL_MAX) { toast(`한 번에 ${VL_MAX}개까지 고를 수 있습니다`, true); return false; }
  // 연령 제한 영상은 쿠키가 없으면 받을 수 없다는 점을 고를 때 알린다
  if (on && v.adult && !vl.selected.has(key)) toast("연령 제한 영상입니다. 그 플랫폼의 쿠키가 없으면 받을 수 없습니다");
  if (on) vl.selected.set(key, { video_no: v.video_no, title: v.title, platform: vl.platform, channel_id: vl.channel });
  else vl.selected.delete(key);
  return true;
}

// 화질·구간을 바꾸려면 그 영상 하나를 분석해 주소로 받기의 분석 카드로 연다
function videoOptions(v) {
  replayMode("url");
  const form = $("analyze-form");
  form.urls.value = v.url; // 받을 주소는 서버(공급자)가 준다
  form.requestSubmit();
}

async function downloadSelected() {
  const items = [...vl.selected.values()];
  if (!items.length) { toast("받을 영상을 고르십시오", true); return; }
  if (!confirm(`고른 ${items.length}개를 기본 화질로 받을까요?`)) return;
  const res = await withDuplicateConfirm((confirmed) => api("POST", "/api/replay/select",
    { items, ...(confirmed ? { confirm: true } : {}) }), (r) => `${r.queued}개를 대기열에 넣었습니다`);
  if (res) { vl.selected.clear(); loadVideos(); loadJobs(); loadReplayState(); }
}

// 새 영상 확인·전체 다시 수집·수집 취소
async function startCollect(mode) {
  if (!vl.channel) return;
  if (mode === "full") {
    const c = vl.collect || {};
    const pages = c.total_pages || (c.count ? Math.ceil(c.count / 30) : 0);
    const time = pages ? `${pages}쪽이라 약 ${Math.max(1, Math.round((pages * VL_SECONDS_PER_PAGE) / 60))}분 걸립니다` : "쪽수를 아직 몰라 걸리는 시간을 알 수 없습니다";
    if (!confirm(`이 채널의 다시보기 목록을 처음부터 다시 수집할까요? ${time}(쪽마다 약 ${VL_SECONDS_PER_PAGE}초). 수집 중에도 받은 달은 볼 수 있습니다.`)) return;
  }
  const res = await act(() => api("POST", vlPath("/collect?") + vlParams(), { mode }),
    (r) => (r.started ? (mode === "full" ? "전체 수집을 시작했습니다" : "새 영상 확인을 시작했습니다") : "이미 수집 중입니다"));
  if (res) { vl.collect = res.collect; showCollect(); }
}

// 다시보기 채널 추가. 공급자가 글을 풀 수 있으면(주소·@핸들) 그대로 보내고, 아니면 채널 추가 칸과
// 같은 규칙으로 ID를 뽑아 보낸다
async function addReplayChannel(event) {
  event.preventDefault();
  const input = $("vl-add-input");
  const note = $("vl-add-note");
  const raw = input.value.trim();
  if (!raw) { note.hidden = true; return; }
  const provider = vlProvider($("vl-add-platform").value);
  const pid = provider ? provider.platform : "";
  const id = provider && provider.resolve ? raw : (pid === defaultPlatform ? parseChannelId(raw) : parseChannelId(raw, channelRule(pid)));
  if (!id) {
    note.textContent = `${provider ? provider.add_hint : "채널 주소를 넣으십시오"}`;
    note.className = "form-error field-note";
    note.hidden = false;
    return;
  }
  note.textContent = "채널 이름을 받는 중…";
  note.className = "muted field-note";
  note.hidden = false;
  try {
    const res = await api("POST", "/api/replay/channels", { channel_id: id, platform: pid });
    note.hidden = true;
    input.value = "";
    toast(`${res.name} 채널을 추가했습니다. 처음 열 때 목록 전체를 수집합니다`);
    vl.channel = res.channel_id;
    vl.platform = res.platform || pid;
    vl.month = null;
    await loadVideoChannels();
  } catch (e) {
    note.textContent = e.message;
    note.className = "form-error field-note";
  }
}

// 지우면 수집 목록이 함께 지워진다. 수집 중이면 서버가 먼저 취소한다
async function removeReplayChannel() {
  const info = vl.chans.get(vlKey(vl.platform, vl.channel));
  if (!info) return;
  const count = vl.collect ? vl.collect.count : 0;
  const busy = vlCollecting() ? " 지금 수집 중인 것은 먼저 취소합니다." : "";
  const rec = info.recording ? " 녹화 채널은 그대로 녹화하고, 이 목록에는 스스로 다시 들어오지 않습니다." : "";
  if (!confirm(`"${info.name}" 다시보기 채널을 지울까요? 수집한 영상 목록 ${count}개도 함께 지워집니다(받은 파일과 받은 기록은 남습니다).${busy}${rec}`)) return;
  const res = await act(() => api("DELETE", vlPath() + vlParams()), "다시보기 채널을 지웠습니다");
  if (res) { vl.channel = ""; vl.month = null; loadVideoChannels(); }
}

async function cancelCollect() {
  if (!vl.channel) return;
  const res = await act(() => api("POST", vlPath("/collect/cancel?") + vlParams()), "수집을 취소했습니다. 받은 줄은 남습니다");
  if (res) refreshVideoList(true);
}

// 대기·진행 전체 취소. 건수와 함께 확인을 받는다

function bindReplayList() {
  replayMode("url"); // 공급자를 받기 전에는 주소로 받기만 보인다(공급자가 없으면 그대로다)
  $("replay-mode-list").addEventListener("click", () => replayMode("list"));
  $("replay-mode-url").addEventListener("click", () => replayMode("url"));
  $("vl-channel").addEventListener("change", () => {
    const info = vl.chans.get($("vl-channel").value);
    if (!info) return;
    vl.channel = info.channel_id;
    vl.platform = info.platform;
    vl.month = null;
    vl.videos = [];
    showTypeFilter();
    openVideoChannel();
  });
  $("vl-add-platform").addEventListener("change", showAddHint);
  $("vl-type").addEventListener("change", () => refreshVideoList(true)); // 종류 필터는 달 단추의 수에도 걸린다
  $("vl-search").addEventListener("input", () => {
    clearTimeout(vl.searchTimer);
    vl.searchTimer = setTimeout(loadVideos, 300);
  });
  $("vl-month-prev").addEventListener("click", () => stepMonth(1));
  $("vl-month-next").addEventListener("click", () => stepMonth(-1));
  $("vl-latest").addEventListener("click", () => startCollect("latest"));
  $("vl-full").addEventListener("click", () => startCollect("full"));
  $("vl-collect-cancel").addEventListener("click", cancelCollect);
  $("vl-add-form").addEventListener("submit", addReplayChannel);
  $("vl-remove").addEventListener("click", removeReplayChannel);
  $("vl-clear").addEventListener("click", () => { vl.selected.clear(); renderVideos(); });
  $("vl-all").addEventListener("change", () => {
    const on = $("vl-all").checked;
    for (const v of vl.videos) if (!toggleVideo(v, on)) break;
    renderVideos();
  });
  $("vl-download").addEventListener("click", downloadSelected);
}

// 채널 목록 방식의 화면 조각은 공급자를 내는 확장이 켜졌을 때만 들어간다. 없으면 붙지 않는다
if (document.getElementById("replay-list-pane")) registerDownloadsMode({
  bind: bindReplayList,
  load: loadVideoChannels,
  // 수집 중에는 달 단추의 수가 늘어난다
  tick: () => { if (vlCollecting() && !$("replay-list-pane").hidden) refreshVideoList(false); },
});
