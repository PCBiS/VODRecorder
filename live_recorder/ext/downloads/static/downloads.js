"use strict";
// downloads 확장의 화면: 다시보기·클립 탭의 주소로 받기와 작업, 위쪽 안내·배지, 설정의 "다시보기" 하위 탭.
// 뼈대(app.js)·설정 화면(settings.js)의 공개 함수(registerView·registerBackground·registerSettingsPart)를 쓴다.
// 다른 받기 방식(replay 확장의 채널 목록)은 registerDownloadsMode로 이 화면에 붙는다.


// ---- 다시보기 ----
let analysis = null;
let openLog = null;
const JOB_LABELS = {
  queued: "대기", running: "진행 중", cancelling: "취소 중", finalizing: "검증 중", completed: "완료",
  failed: "실패", cancelled: "취소", interrupted: "중단", paused: "일시정지",
};
const STAGE_LABELS = { analyzing: "주소 확인", downloading: "받는 중", validating: "검증", finalizing: "마무리" };
// 일시정지한 작업의 이름(멈춘 이유별)
const PAUSE_LABELS = { user: "일시정지", global: "전체 정지", window: "시간대 밖", recording: "녹화 중 멈춤", space: "공간 부족", queue: "재개 대기", extension: "확장 꺼짐" };
let replayGate = null; // 지금의 입장 상태(/api/replay/status의 admission)

async function loadReplayState() {
  const st = await act(() => api("GET", "/api/replay/status"));
  if (!st) return null;
  $("replay-state").textContent = st.error ? st.error
    : `저장 폴더 ${st.folder} · 연결 ${st.connections}개`;
  showReplayBadge(st.active);
  showAdmission(st);
  return st;
}

// 위쪽 안내: 새 다운로드를 시작하지 않는 이유와 전체 일시정지 스위치
function admissionText(gate) {
  if (!gate || gate.open) return "";
  if (gate.reason === "global") return "전체 일시정지 중입니다. 받던 작업은 부분 파일을 남겼고, 전체 재개를 누르면 대기열 순서대로 이어받습니다.";
  if (gate.reason === "space") return `다시보기 폴더의 여유 공간이 설정한 최소 여유 공간${gate.space_need ? `과 받는 데 더 들 공간(${bytes(gate.space_need)})의 합` : ""}보다 적어 다운로드를 멈췄습니다. 공간이 생기면 다시 받습니다.`;
  if (gate.reason === "recording") return `실시간 녹화 중이라 다운로드를 멈췄습니다(녹화 ${gate.recording}·마무리 ${gate.finalizing}). 녹화와 마무리가 모두 끝나면 다시 받습니다.`;
  if (gate.reason === "window") {
    if (gate.error) return `시간대 규칙을 읽지 못해 다운로드를 멈췄습니다: ${gate.error}`;
    const next = gate.next_open_at ? ` ${seoulClock(gate.next_open_at)}에 다시 시작합니다.` : "";
    const finish = gate.end_action === "finish" ? " 받던 작업은 끝까지 받습니다." : "";
    return `다운로드 시간대 밖입니다.${next}${finish}`;
  }
  return gate.reason_label || "";
}

function showAdmission(st) {
  replayGate = st.admission || null;
  const text = admissionText(replayGate);
  $("replay-admission").textContent = text;
  $("replay-admission").hidden = !text;
  const paused = Boolean(replayGate && replayGate.paused);
  const button = $("replay-pause-all");
  button.textContent = paused ? "전체 재개" : "전체 일시정지";
  button.setAttribute("aria-pressed", String(paused));
}

async function togglePauseAll() {
  const paused = Boolean(replayGate && replayGate.paused);
  const st = await act(() => api("PUT", "/api/replay/control", { paused: !paused }),
    paused ? "대기열 순서대로 다시 받습니다" : "받던 작업은 부분 파일을 남기고 멈춥니다");
  if (st) { showAdmission(st); showReplayBadge(st.active); loadJobs(); }
}

// 다시보기 탭에 대기·진행 중인 작업 수
function showReplayBadge(count) {
  const badge = $("replay-badge");
  badge.textContent = count ? String(count) : "";
  badge.hidden = !count;
}

// 분석 결과와 작업 줄에 보이는 종류 이름(켜진 종류의 이름, 모르면 종류 값)
function kindTitle(kind) {
  const found = downloadKinds.find((k) => k.kind === (kind || "video"));
  return found ? found.title : (kind || "");
}

function videoCard(item, index) {
  const pick = el("input", { type: "checkbox", "aria-label": "받기 선택" });
  pick.checked = !item.error;
  pick.disabled = Boolean(item.error);
  const thumb = item.thumbnail ? el("img", { src: item.thumbnail, alt: "", loading: "lazy", referrerpolicy: "no-referrer" })
    : el("div", { class: "thumb-empty" });
  const info = el("div", {},
    el("div", { class: "card-title" }, pick, el("strong", { text: item.title || item.url })),
    el("div", { class: "card-meta", text: [item.channel, item.open_date, item.duration_seconds ? hms(item.duration_seconds) : "",
      kindTitle(item.kind), item.mode === "hls" && (item.kind || "video") === "video" ? "빠른 다시보기" : ""].filter(Boolean).join(" · ") }),
    el("div", { class: "card-meta", text: item.url }));
  if (item.error) {
    info.append(el("div", { class: "card-error", text: item.error }));
  } else {
    const quality = el("select", { "aria-label": "화질" }, ...item.qualities.map((q) => el("option", { value: q.id, text: q.label })));
    const opts = el("div", { class: "opts" }, el("label", {}, "화질", quality));
    let section = null;
    if (item.supports.section) {
      section = el("input", { type: "text", placeholder: "00:00:00~00:10:00", "aria-label": "구간", spellcheck: "false" });
      opts.append(el("label", {}, "구간(비우면 전체)", section));
    } else {
      opts.append(el("span", { class: "muted", text: "이 영상은 전체만 받습니다" }));
    }
    info.append(opts);
    item._ui = { pick, quality, section };
  }
  return el("article", { class: "card video-card", dataset: { index: String(index) } }, el("div", { class: "video" }, thumb, info));
}

function showAnalysis(result) {
  analysis = result;
  const box = $("analysis");
  if (!result || !result.items.length) { box.replaceChildren(); $("enqueue-row").hidden = true; return; }
  box.replaceChildren(...result.items.map(videoCard));
  $("enqueue-row").hidden = !result.items.some((i) => !i.error);
}

async function runAnalysis(event) {
  event.preventDefault();
  const urls = lines(event.target.urls.value);
  if (!urls.length) { toast("주소를 입력하십시오", true); return; }
  $("analyze-result").textContent = "분석 중…";
  showAnalysis(null);
  const started = await act(() => api("POST", "/api/replay/analyze", { urls }));
  if (!started) { $("analyze-result").textContent = ""; return; }
  for (let i = 0; i < 120; i += 1) {
    await new Promise((r) => setTimeout(r, 1000));
    const res = await act(() => api("GET", `/api/replay/analyses/${started.id}`));
    if (!res) break;
    if (res.status === "completed") {
      $("analyze-result").textContent = `${res.items.length}개 분석`;
      showAnalysis(res);
      return;
    }
    if (res.status === "failed" || res.status === "interrupted") {
      $("analyze-result").textContent = res.error || "분석하지 못했습니다";
      return;
    }
  }
}

// 중복 거부(409)면 제목을 보이고 확인을 받은 뒤 확인 표시를 붙여 다시 보낸다
async function withDuplicateConfirm(send, okMessage) {
  try {
    const result = await send(false);
    toast(okMessage(result));
    return result;
  } catch (e) {
    const titles = e.data && e.data.duplicates;
    if (e.status !== 409 || !titles) { toast(e.message, true); return null; }
    if (!confirm(`이미 받았거나 대기·진행 중인 영상이 있습니다.\n\n${titles.join("\n")}\n\n그래도 넣을까요?`)) return null;
    return act(() => send(true), okMessage);
  }
}

async function enqueueSelected() {
  if (!analysis) return;
  const items = [];
  for (const item of analysis.items) {
    if (!item._ui || !item._ui.pick.checked) continue;
    const section = item._ui.section ? item._ui.section.value.trim() : "";
    items.push({ url: item.url, quality: item._ui.quality.value, ...(section ? { section } : {}) });
  }
  if (!items.length) { toast("받을 영상을 고르십시오", true); return; }
  const key = Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
  const res = await withDuplicateConfirm((confirmed) => api("POST", "/api/replay/jobs",
    { analysis_id: analysis.id, items, key, ...(confirmed ? { confirm: true } : {}) }), (r) => `${r.jobs.length}개를 대기열에 넣었습니다`);
  if (res) loadJobs();
}

async function cancelAllJobs() {
  const st = await act(() => api("GET", "/api/replay/status"));
  if (!st || !st.active) { toast("대기·진행 중인 작업이 없습니다"); return; }
  // 일시정지한 작업은 넣지 않는다(부분 파일이 실수로 지워지지 않게)
  const paused = st.paused_jobs ? ` 일시정지한 ${st.paused_jobs}개는 그대로 둡니다.` : "";
  if (!confirm(`대기·진행 중인 작업 ${st.active}개를 모두 취소할까요?${paused}`)) return;
  const res = await act(() => api("POST", "/api/replay/cancel-active"), (r) => `${r.cancelled}개를 취소했습니다`);
  if (res) { loadJobs(); loadReplayState(); }
}

function jobRow(job) {
  const paused = job.status === "paused";
  const status = paused ? (PAUSE_LABELS[job.pause_reason] || JOB_LABELS.paused) : (JOB_LABELS[job.status] || job.status);
  const stage = job.status === "running" ? (STAGE_LABELS[job.stage] || "") : "";
  const cls = job.status === "completed" ? "ok" : job.status === "failed" ? "bad" : ["running", "finalizing", "cancelling", "paused"].includes(job.status) ? "warn" : "";
  const statusCell = el("td", {}, el("span", { class: `badge ${cls}`, text: status }));
  if (stage) statusCell.append(el("div", { class: "muted", text: stage }));
  if (job.error) statusCell.append(el("div", { class: "muted", text: job.error }));
  if (job.blocked) statusCell.append(el("div", { class: "muted", text: job.blocked })); // 꺼진 확장의 작업
  // 멈춘 이유와 기다리는 이유. 이어받기·처음부터 받기 기록
  if (paused && job.pause_reason !== "queue") statusCell.append(el("div", { class: "muted", text: `멈춘 이유: ${({ user: "사용자", global: "전체 일시정지", window: "시간대 밖", recording: "실시간 녹화 중", space: "여유 공간 부족", extension: "확장 꺼짐" })[job.pause_reason] || job.pause_reason}` }));
  if ((job.status === "queued" || (paused && job.pause_reason !== "user")) && replayGate && !replayGate.open) {
    statusCell.append(el("div", { class: "muted", text: `기다리는 이유: ${replayGate.reason_label}` }));
  }
  if (paused && job.partial_bytes) statusCell.append(el("div", { class: "muted", text: `부분 파일 ${bytes(job.partial_bytes)}` }));
  if (job.resume_note) statusCell.append(el("div", { class: "muted job-note", text: job.resume_note }));
  const progress = job.progress == null ? null : Math.max(0, Math.min(100, job.progress));
  const bar = el("div", { class: "bar" }, el("span", {}));
  bar.firstChild.style.width = `${progress || 0}%`;
  // 전송 속도와 남은 시간(모르면 비운다)
  const amount = job.downloaded_bytes ? (job.total_bytes ? `${bytes(job.downloaded_bytes)} / ${bytes(job.total_bytes)}` : bytes(job.downloaded_bytes)) : "";
  const running = job.status === "running";
  const progressCell = el("td", { class: "num" }, bar,
    el("div", { class: "muted", text: [progress == null ? "" : `${progress.toFixed(1)}%`, amount,
      running && job.speed_bps ? `${bytes(job.speed_bps)}/s` : "", running && job.eta_seconds != null ? `남은 ${hms(job.eta_seconds)}` : "",
      job.attempt > 1 ? `${job.attempt}번째 시도` : ""].filter(Boolean).join(" · ") }));
  const actions = el("div", { class: "actions" });
  if (job.status === "queued" || (running && job.stage !== "validating")) {
    actions.append(el("button", { class: "btn small", text: "일시정지", dataset: { action: "pause" },
      onclick: () => act(() => api("POST", `/api/replay/jobs/${job.id}/pause`), "일시정지했습니다").then(loadJobs) }));
  }
  if (paused) {
    actions.append(el("button", { class: "btn small", text: "재개", dataset: { action: "resume" },
      onclick: () => act(() => api("POST", `/api/replay/jobs/${job.id}/resume`), "대기열 순서대로 이어받습니다").then(loadJobs) }));
  }
  if (["queued", "running", "finalizing", "paused"].includes(job.status)) {
    actions.append(el("button", { class: "btn small danger", text: "취소", dataset: { action: "cancel" },
      onclick: () => act(() => api("POST", `/api/replay/jobs/${job.id}/cancel`), paused ? "취소하고 부분 파일을 지웠습니다" : "취소했습니다").then(loadJobs) }));
  }
  if (["failed", "cancelled", "interrupted"].includes(job.status)) {
    actions.append(el("button", { class: "btn small", text: "재시도", dataset: { action: "retry" },
      onclick: () => act(() => api("POST", `/api/replay/jobs/${job.id}/retry`), "다시 대기열에 넣었습니다").then(loadJobs) }));
  }
  if (job.can_download) actions.append(el("a", { class: "btn small", href: `/api/replay/jobs/${job.id}/file`, text: "내려받기" }));
  actions.append(el("button", { class: "btn small ghost", text: "로그", onclick: () => showLog(job) }));
  return el("tr", { dataset: { job: job.id, status: job.status } },
    el("td", { class: "file" }, el("div", { text: job.filename || job.title || job.url }),
      el("div", { class: "muted", text: [kindTitle(job.kind), job.url].filter(Boolean).join(" · ") })),
    el("td", { text: [job.quality_label || job.quality, job.section || "전체"].join(" · ") }),
    statusCell, progressCell, el("td", {}, actions));
}

let jobFilter = "attention"; // 확인 필요가 기본이다

async function loadJobs() {
  const data = await act(() => api("GET", `/api/replay/jobs?status=${jobFilter}`));
  if (!data) return;
  for (const span of document.querySelectorAll("[data-count]")) span.textContent = `(${data.counts[span.dataset.count] || 0})`;
  $("jobs-body").replaceChildren(...(data.jobs.length ? data.jobs.map(jobRow)
    : [el("tr", {}, el("td", { colspan: "5", class: "muted", text: "작업이 없습니다." }))]));
  if (openLog) showLog(data.jobs.find((j) => j.id === openLog) || null, true);
}

async function showLog(job, refresh) {
  const box = $("job-log");
  if (!job || (!refresh && openLog === job.id && !box.hidden)) { box.hidden = true; openLog = null; return; }
  openLog = job.id;
  const data = await act(() => api("GET", `/api/replay/jobs/${job.id}/logs`));
  if (!data) return;
  box.textContent = `${job.filename || job.title || job.url}\n` + data.logs.map((e) => `${formatWhen(e.at, { seconds: true })}  ${e.message}`).join("\n");
  box.hidden = false;
}

// ---- 작업 종류: 켜진 확장이 등록한 종류에 따라 탭·제목·주소 칸을 맞춘다 ----
const DOWNLOAD_MODES = []; // 다른 받기 방식(replay 확장의 채널 목록)
let downloadKinds = [];

function registerDownloadsMode(mode) {
  DOWNLOAD_MODES.push(mode);
}

function downloadsTitle() {
  // 켜진 작업 종류가 하나면 그 이름, 둘 이상이거나 없으면 "다운로드"
  return downloadKinds.length === 1 ? downloadKinds[0].title : "다운로드";
}

function showKinds() {
  const title = downloadsTitle();
  $("replay-tab-label").textContent = title;
  $("replay-title").textContent = title;
  const subtab = $("stab-replay"); // 설정의 하위 탭 이름도 같은 규칙이다
  if (subtab) subtab.textContent = title;
  const heading = $("settings-replay-title");
  if (heading) heading.textContent = title;
  // 종류 이름에 플랫폼 이름이 이미 들어 있으면(예: "씨미 다시보기") 다시 붙이지 않는다
  const names = downloadKinds.map((k) => { const p = platformOf(k.platform); return `${p && !k.title.includes(p.name) ? `${p.name} ` : ""}${k.title}`; });
  $("analyze-label").textContent = `${names.join("·") || "영상"} 주소(한 줄에 하나, 10개까지)`;
  // 주소 칸 하나. 붙여 넣은 주소로 종류를 알아본다. 예시는 켜진 종류의 것을 모두 보인다
  $("analyze-form").urls.placeholder = downloadKinds.map((k) => k.example).filter(Boolean).join("\n");
}

async function loadReplay() {
  const st = await loadReplayState();
  if (st && st.ready) { loadJobs(); for (const mode of DOWNLOAD_MODES) mode.load(); }
}

function tickReplay() {
  loadJobs();
  for (const mode of DOWNLOAD_MODES) if (mode.tick) mode.tick();
}

async function bindDownloads() {
  $("analyze-form").addEventListener("submit", runAnalysis);
  $("enqueue").addEventListener("click", enqueueSelected);
  $("jobs-cancel-all").addEventListener("click", cancelAllJobs);
  $("replay-pause-all").addEventListener("click", togglePauseAll);
  for (const btn of document.querySelectorAll("[data-filter]")) {
    btn.addEventListener("click", () => {
      jobFilter = btn.dataset.filter;
      for (const b of document.querySelectorAll("[data-filter]")) b.setAttribute("aria-selected", String(b === btn));
      loadJobs();
    });
  }
  if (!DOWNLOAD_MODES.length) $("replay-url-pane").hidden = false; // 채널 목록이 없으면 주소로 받기만 있다
  for (const mode of DOWNLOAD_MODES) mode.bind();
  const info = await api("GET", "/api/replay/kinds");
  downloadKinds = (info && Array.isArray(info.kinds)) ? info.kinds : [];
  showKinds();
}

registerView("replay", { load: loadReplay, tick: tickReplay, bind: bindDownloads });
// 탭 배지와 위쪽 안내는 어느 화면에서든 갱신한다(보기 전용 모드는 빼고)
registerBackground(() => {
  api("GET", "/api/replay/status").then((st) => { showReplayBadge(st.active); showAdmission(st); }).catch(() => null);
});

// ---- 다운로드 허용 시간대 규칙 ----
const WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"];
const WINDOW_MAX = 7;

function windowRuleRow(rule) {
  const r = rule || { days: [0, 1, 2, 3, 4, 5, 6], start: "09:00", end: "19:00" };
  const days = el("div", { class: "days" }, ...WEEKDAYS.map((name, index) => {
    const box = el("input", { type: "checkbox", value: String(index), "aria-label": `${name}요일` });
    box.checked = r.days.includes(index);
    return el("label", {}, box, el("span", { text: name }));
  }));
  const start = el("input", { type: "time", required: true, "aria-label": "시작", dataset: { field: "start" } });
  start.value = r.start;
  const end = el("input", { type: "time", required: true, "aria-label": "끝", dataset: { field: "end" } });
  end.value = r.end;
  const row = el("div", { class: "window-rule" }, days, start, el("span", { text: "–" }), end);
  row.append(el("button", { class: "btn small ghost", type: "button", text: "삭제", onclick: () => row.remove() }));
  return row;
}

function collectWindowRules() {
  return Array.from($("window-rules").children).map((row) => ({
    days: Array.from(row.querySelectorAll(".days input:checked")).map((box) => Number(box.value)),
    start: row.querySelector("[data-field=start]").value,
    end: row.querySelector("[data-field=end]").value,
  }));
}

// 시각은 Asia/Seoul로 보인다
function seoulClock(iso) {
  return formatWhen(iso);
}

function showWindowNow(gate) {
  let text = "시간대를 쓰지 않습니다. 언제든 받습니다.";
  if (gate && gate.window_enabled) {
    if (gate.error) text = `저장된 규칙을 읽지 못해 받지 않습니다: ${gate.error}`;
    else if (gate.reasons && gate.reasons.includes("window")) text = `지금은 시간대 밖입니다.${gate.next_open_at ? ` ${seoulClock(gate.next_open_at)}에 시작한다.` : ""}`;
    else text = `지금은 시간대 안입니다.${gate.closes_at ? ` ${seoulClock(gate.closes_at)}까지.` : ""}`;
  }
  $("window-now").textContent = text;
}

// ---- 설정의 "다시보기" 하위 탭. 이 확장의 설정 키만 다룬다 ----
registerSettingsPart("replay", {
  form: "settings-replay-form",
  values(f) {
    return {
      replay_dir: f.replay_dir.value.trim(),
      replay_min_free_bytes: Math.round(Number(f.replay_min_free_gib.value) * 1024 ** 3),
      replay_connections: Number(f.replay_connections.value),
      replay_pause_while_recording: f.replay_pause_while_recording.checked,
      replay_window_enabled: f.replay_window_enabled.checked,
      replay_window_end_action: f.replay_window_end_action.value,
      replay_window_rules: collectWindowRules(),
      // 같은 탭에 다른 확장이 넣은 설정 칸(예: youtube_video_codec)
      ...Object.fromEntries(Array.from(f.querySelectorAll("[data-ext-setting]")).map((n) => [n.dataset.extSetting, n.value])),
    };
  },
  fill(f, s) {
    f.replay_dir.value = s.replay_dir;
    f.replay_min_free_gib.value = gib(s.replay_min_free_bytes);
    f.replay_connections.value = s.replay_connections;
    // 녹화 중 멈춤과 다운로드 허용 시간대
    f.replay_pause_while_recording.checked = s.replay_pause_while_recording;
    f.replay_window_enabled.checked = s.replay_window_enabled;
    f.replay_window_end_action.value = s.replay_window_end_action;
    $("window-rules").replaceChildren(...s.replay_window_rules.map(windowRuleRow));
    for (const n of f.querySelectorAll("[data-ext-setting]")) if (s[n.dataset.extSetting] != null) n.value = s[n.dataset.extSetting];
  },
  async load() {
    const st = await act(() => api("GET", "/api/replay/status"));
    if (st) showWindowNow(st.admission);
  },
  bind() {
    $("window-add").addEventListener("click", () => {
      if ($("window-rules").children.length >= WINDOW_MAX) { toast(`규칙은 ${WINDOW_MAX}개까지입니다`, true); return; }
      $("window-rules").append(windowRuleRow());
    });
  },
});
