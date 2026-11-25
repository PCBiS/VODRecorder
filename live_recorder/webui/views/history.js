"use strict";
// 기록 화면. 7일 타임라인과 녹화 목록. 뼈대(app.js)의 공통 함수를 쓰고 registerView로 자기 화면을 등록한다.

// ---- 기록 ----
const END_LABELS = { broadcast_end: "방송 종료", user_stop: "사용자 중지", stalled: "정체", error: "오류", server_shutdown: "서버 종료" };
const FINAL_LABELS = { done: "완료", failed: "실패", pending: "대기", running: "진행 중", skipped: "없음" };
const DAY_MS = 86400000;
const KST_MS = 9 * 3600000;
const NOON_MS = 12 * 3600000;
const TIMELINE_DAYS = 7;
let weekOffset = 0; // 0이면 이번 주(오늘 줄까지). 1이면 그 앞 7일
let earliestMs = null; // 기록이 있을 수 있는 가장 이른 시각(서버가 준다). 모르면 null
const openRows = new Set();

// 한 줄은 KST 정오부터 다음 날 정오까지다. 줄의 시작 시각(UTC 밀리초)
function rowStartOf(ms) {
  return Math.floor((ms + KST_MS - NOON_MS) / DAY_MS) * DAY_MS + NOON_MS - KST_MS;
}

function kstParts(ms) {
  const d = new Date(ms + KST_MS);
  return { y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate(), w: "일월화수목금토"[d.getUTCDay()], h: d.getUTCHours(), mi: d.getUTCMinutes(), s: d.getUTCSeconds() };
}

// 타임라인 줄과 주 선택 목록의 짧은 날짜 눈금
function dayLabel(ms) {
  const p = kstParts(ms);
  return `${p.m}/${p.d} (${p.w})`;
}

function kstLabel(ms, withSeconds) {
  return formatWhen(ms, { seconds: withSeconds });
}

// 세션 구간: 파일이 담은 방송 시각(첫·마지막 조각)이 녹화 시각과 가까우면(되감기 한도 안) 그것, 아니면 녹화 시작·끝.
// 진행 중이면 끝은 지금이다
const MEDIA_SLACK_MS = 2 * 3600000;
function near(mediaIso, wallMs) {
  const t = mediaIso ? Date.parse(mediaIso) : NaN;
  return Number.isFinite(t) && Math.abs(t - wallMs) <= MEDIA_SLACK_MS ? t : wallMs;
}

function span(s, now) {
  const wallStart = Date.parse(s.started_at);
  const start = near(s.first_media_time, wallStart);
  let end = s.ended_at ? near(s.last_media_time, Date.parse(s.ended_at)) : now;
  if (!(end > start)) end = start;
  return [start, end];
}

function sessionSeconds(s, now) {
  if (s.media_seconds) return s.media_seconds;
  const [a, b] = span(s, now);
  return (b - a) / 1000;
}

// 같은 방송(방송 ID)에서 나뉜 세션을 묶어 앞 줄에 "끊겨 재접속", 뒤 줄에 "이어 받음"을 단다
function pairMarks(sessions, now) {
  const groups = new Map();
  for (const s of sessions) if (s.group != null) {
    if (!groups.has(s.group)) groups.set(s.group, []);
    groups.get(s.group).push(s);
  }
  const marks = new Map();
  for (const list of groups.values()) {
    if (list.length < 2) continue;
    list.sort((a, b) => span(a, now)[0] - span(b, now)[0] || a.id - b.id);
    list.forEach((s, i) => marks.set(s.id, { before: i < list.length - 1, after: i > 0 }));
  }
  return marks;
}

function endLabel(s) {
  return s.ended_at ? (END_LABELS[s.end_reason] || s.end_reason || "") : "녹화 중";
}

function barKind(s, marks) {
  if (!s.ended_at) return "live";
  return marks.has(s.id) ? "split" : "normal";
}

function sessionSummary(s, now) {
  const [a, b] = span(s, now);
  return `${s.channel_name || "지운 채널"} · ${s.title || "제목 없음"}\n시작 ${kstLabel(a, true)} · 끝 ${s.ended_at ? kstLabel(b, true) : "녹화 중"}\n길이 ${hms(sessionSeconds(s, now))} · ${endLabel(s)}`;
}

function goToSession(s, now) {
  const detail = $("tl-detail");
  detail.textContent = sessionSummary(s, now);
  detail.hidden = false;
  const row = document.getElementById(`sess-${s.id}`);
  if (row) {
    row.open = true;
    openRows.add(s.id);
    for (const other of document.querySelectorAll(".srow.focus")) other.classList.remove("focus");
    row.classList.add("focus");
    row.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

function timelineFor(name, sessions, rows, marks, now) {
  const block = el("div", { class: "tl-channel" }, el("h3", { text: name }));
  const axis = el("div", { class: "tl-row tl-axis", "aria-hidden": "true" }, el("span", { class: "tl-day" }),
    el("div", { class: "tl-track" }, ...["12시", "18시", "0시", "6시", "12시"].map((t, i) => {
      const tick = el("span", { class: "tl-tick", text: t });
      tick.style.left = `${i * 25}%`;
      return tick;
    })), el("span", { class: "tl-total" }));
  block.append(axis);
  for (const start of rows) {
    const end = start + DAY_MS;
    const track = el("div", { class: "tl-track" });
    let total = 0;
    for (const s of sessions) {
      const [a, b] = span(s, now);
      const lo = Math.max(a, start);
      const hi = Math.min(b, end);
      if (hi <= lo && !(a >= start && a < end)) continue;
      total += Math.max(0, hi - lo);
      const bar = el("button", { class: `tl-bar ${barKind(s, marks)}`, type: "button", title: sessionSummary(s, now),
        "aria-label": sessionSummary(s, now).replace(/\n/g, ", "), onclick: () => goToSession(s, now) });
      bar.style.left = `${((lo - start) / DAY_MS) * 100}%`;
      bar.style.width = `${(Math.max(0, hi - lo) / DAY_MS) * 100}%`;
      track.append(bar);
    }
    const day = el("span", { class: "tl-day", text: dayLabel(start) });
    const totalCell = el("span", { class: "tl-total", text: total ? hms(total / 1000) : "방송 없음" });
    if (!total) totalCell.classList.add("muted");
    block.append(el("div", { class: "tl-row" }, day, track, totalCell));
  }
  return block;
}

function sessionRow(s, marks, now) {
  const [a] = span(s, now);
  const mark = marks.get(s.id);
  const tags = el("span", { class: "tags" });
  if (!s.ended_at) tags.append(el("span", { class: "tag live", text: "녹화 중" }));
  if (mark && mark.after) tags.append(el("span", { class: "tag split", text: "이어 받음" }));
  if (mark && mark.before) tags.append(el("span", { class: "tag split", text: "끊겨 재접속" }));
  const summary = el("summary", {},
    el("span", { class: "s-time", text: kstLabel(a) }),
    el("span", { class: "s-title" }, el("span", { class: "s-ch", text: s.channel_name || "지운 채널" }),
      el("span", { class: "s-name", title: s.title || "", text: s.title || "제목 없음" })),
    el("span", { class: "s-len num", text: hms(sessionSeconds(s, now)) }),
    el("span", { class: "s-size num", text: s.bytes ? bytes(s.bytes) : "" }),
    el("span", { class: "s-end" }, endLabel(s), tags),
  );
  const finalText = FINAL_LABELS[s.finalize_status] || (s.ended_at ? "" : "녹화 중");
  const facts = [
    ["파일", s.file || "-"],
    ["주소 갱신", s.url_refreshes != null ? `${s.url_refreshes}회` : "-"],
    ["종료코드", s.exit_code != null ? String(s.exit_code) : "-"],
    ["마무리", finalText + (!viewOnly && s.finalize_detail ? ` · ${s.finalize_detail}` : "")],
  ];
  // 타임머신이 없는 플랫폼의 기록에는 수신 방식(타임머신·일반)을 내지 않는다
  if (s.timemachine !== false) facts.splice(1, 0, ["수신 방식", s.source === "timemachine" ? "타임머신" : s.source === "live" ? "일반" : "-"]);
  // 이 파일에 받은 영상 코덱
  if (s.video_codec) facts.splice(1, 0, ["영상 코덱", { h264: "H.264", hevc: "HEVC", av1: "AV1", vp9: "VP9" }[s.video_codec] || s.video_codec]);
  if (!viewOnly && s.end_detail) facts.push(["종료 상세", s.end_detail]);
  if (!viewOnly && s.note) facts.push(["메모", s.note]);
  const dl = el("dl", { class: "s-facts" });
  for (const [k, v] of facts) dl.append(el("dt", { text: k }), el("dd", { text: v }));
  const row = el("details", { class: `srow ${s.finalize_status === "failed" || s.end_reason === "error" ? "bad" : ""}`, id: `sess-${s.id}` }, summary, dl);
  if (openRows.has(s.id)) row.open = true;
  row.addEventListener("toggle", () => { if (row.open) openRows.add(s.id); else openRows.delete(s.id); });
  return row;
}

// ---- 주 이동 한계와 주 선택 ----
const WEEK_MS = TIMELINE_DAYS * DAY_MS;

// 가장 오래된 주의 번호. 그 주의 7줄 안에 설치한 날의 줄이 있다. 설치 시각을 모르면 이번 주만
function maxWeekOffset(now, earliest) {
  if (earliest == null || !Number.isFinite(earliest)) return 0;
  return Math.max(0, Math.floor((rowStartOf(now) - rowStartOf(earliest)) / WEEK_MS));
}

// 줄의 날짜(KST). 줄은 정오에 시작하므로 시작 시각의 날짜가 그 줄의 날짜다
function rowDate(start) {
  const { y, m } = kstParts(start);
  return { y, m };
}

// 그 달에 하루라도 걸친 주를 오래된 것부터. disabled는 설치 전이라 고를 수 없는 주다
function weeksOfMonth(year, month, now, earliest) {
  const newestRow = rowStartOf(now);
  const max = maxWeekOffset(now, earliest);
  const monthStart = Date.UTC(year, month - 1, 1) - KST_MS + NOON_MS; // 1일 줄의 시작
  const monthEnd = Date.UTC(year, month, 1) - KST_MS + NOON_MS; // 다음 달 1일 줄의 시작
  // 주(마지막 줄 last, 첫 줄 last-6일)가 달과 겹치려면 last >= 1일 줄, 첫 줄 <= 말일 줄. 줄 수로 센다
  const daysToMonthEnd = Math.round((newestRow - (monthEnd - DAY_MS)) / DAY_MS);
  const lo = Math.max(0, Math.ceil((daysToMonthEnd - (TIMELINE_DAYS - 1)) / TIMELINE_DAYS));
  const hi = Math.floor(Math.round((newestRow - monthStart) / DAY_MS) / TIMELINE_DAYS);
  const out = [];
  for (let offset = hi; offset >= lo; offset -= 1) {
    const last = newestRow - offset * WEEK_MS;
    out.push({ offset, from: last - (TIMELINE_DAYS - 1) * DAY_MS, to: last, disabled: offset > max });
  }
  return out;
}

function renderWeekList() {
  const now = Date.now();
  const year = Number($("week-year").value);
  const month = Number($("week-month").value);
  const weeks = weeksOfMonth(year, month, now, earliestMs);
  $("week-list").replaceChildren(...weeks.map((w, i) => {
    const b = el("button", { class: "btn week-item", type: "button", text: `${i + 1}주차 · ${dayLabel(w.from)} – ${dayLabel(w.to)}`,
      onclick: () => { weekOffset = w.offset; $("week-dialog").close(); loadHistory(); } });
    if (w.disabled) { b.disabled = true; b.title = "설치 전이라 기록이 없습니다."; }
    if (w.offset === weekOffset) b.setAttribute("aria-current", "true");
    return b;
  }));
  $("week-note").textContent = earliestMs == null ? "" : `${formatWhen(earliestMs, { time: false })}부터의 기록을 볼 수 있습니다.`;
}

// 고를 수 있는 달만 낸다. 첫 달은 설치한 날의 줄이 든 달, 끝 달은 오늘 줄의 달이다
function fillWeekMonths(keep) {
  const now = Date.now();
  const first = rowDate(earliestMs == null ? rowStartOf(now) : rowStartOf(earliestMs));
  const last = rowDate(rowStartOf(now));
  const year = Number($("week-year").value);
  const lo = year === first.y ? first.m : 1;
  const hi = year === last.y ? last.m : 12;
  const months = [];
  for (let m = lo; m <= hi; m += 1) months.push(el("option", { value: String(m), text: `${m}월` }));
  $("week-month").replaceChildren(...months);
  $("week-month").value = String(Math.min(hi, Math.max(lo, keep || hi)));
  renderWeekList();
}

function openWeekPicker() {
  const now = Date.now();
  const first = rowDate(earliestMs == null ? rowStartOf(now) : rowStartOf(earliestMs));
  const last = rowDate(rowStartOf(now));
  const shown = rowDate(rowStartOf(now) - weekOffset * WEEK_MS); // 지금 보는 주의 마지막 줄
  const years = [];
  for (let y = last.y; y >= first.y; y -= 1) years.push(el("option", { value: String(y), text: `${y}년` }));
  $("week-year").replaceChildren(...years);
  $("week-year").value = String(shown.y);
  fillWeekMonths(shown.m);
  $("week-dialog").showModal();
}

// 시각 칸의 너비는 고른 날짜·시각 모양에 따라 달라진다. 줄마다 따로 잡힌 너비 가운데 가장 넓은 것으로 모든 줄을 맞춘다
function fitTimeColumn() {
  const box = $("session-list");
  box.style.removeProperty("--s-time");
  let widest = 0;
  for (const cell of box.querySelectorAll(".s-time")) widest = Math.max(widest, cell.getBoundingClientRect().width);
  if (widest > 0) box.style.setProperty("--s-time", `${Math.ceil(widest)}px`);
}

async function loadHistory() {
  const now = Date.now();
  weekOffset = Math.min(weekOffset, maxWeekOffset(now, earliestMs));
  const newest = rowStartOf(now) - weekOffset * WEEK_MS;
  const rows = Array.from({ length: TIMELINE_DAYS }, (_, i) => newest - i * DAY_MS); // 최근 날이 위
  const from = rows[rows.length - 1];
  const to = newest + DAY_MS;
  // 되감기로 파일이 녹화 시작보다 앞선 구간을 담을 수 있어 하루씩 넓혀 받는다
  const query = `start=${encodeURIComponent(new Date(from - DAY_MS).toISOString())}&end=${encodeURIComponent(new Date(to + DAY_MS).toISOString())}`;
  let data;
  try {
    if (!channels.length) await fetchChannels().catch(() => null);
    data = await api("GET", (viewOnly ? "/api/view/sessions?" : "/api/sessions?") + query);
  } catch (e) { toast(e.message, true); return; }
  showDisk(data.disk);
  const earliest = Date.parse(data.earliest);
  earliestMs = Number.isFinite(earliest) ? earliest : null;
  if (weekOffset > maxWeekOffset(now, earliestMs)) { weekOffset = maxWeekOffset(now, earliestMs); return loadHistory(); }
  const sessions = data.sessions.filter((s) => { const [a, b] = span(s, now); return b >= from && a < to; });
  const marks = pairMarks(data.sessions, now);
  $("tl-label").textContent = `${formatWhen(from, { time: false })} – ${formatWhen(newest, { time: false })}`;
  $("tl-prev").disabled = weekOffset >= maxWeekOffset(now, earliestMs); // 설치 전 주로는 가지 않는다
  $("tl-next").disabled = weekOffset === 0;
  // 채널마다 타임라인 하나. 등록 순서(ch)대로, 지운 채널의 기록은 끝에
  const names = new Map(channels.map((c, i) => [i + 1, c.name]));
  for (const s of sessions) if (!names.has(s.ch)) names.set(s.ch, s.channel_name || "지운 채널");
  const blocks = [...names.entries()].sort((x, y) => (x[0] || 1e9) - (y[0] || 1e9))
    .map(([ch, name]) => timelineFor(name, sessions.filter((s) => s.ch === ch), rows, marks, now));
  $("timeline").replaceChildren(...(blocks.length ? blocks : [el("p", { class: "muted", text: "등록한 채널이 없습니다." })]));
  const list = sessions.slice().sort((x, y) => span(y, now)[0] - span(x, now)[0] || y.id - x.id);
  $("session-list").replaceChildren(...(list.length ? list.map((s) => sessionRow(s, marks, now))
    : [el("p", { class: "empty", text: "이 기간에는 녹화 기록이 없습니다." })]));
  fitTimeColumn();
}

function bindHistoryCommon() {
  $("tl-prev").addEventListener("click", () => { if (weekOffset < maxWeekOffset(Date.now(), earliestMs)) { weekOffset += 1; loadHistory(); } });
  $("tl-pick").addEventListener("click", openWeekPicker);
  $("week-year").addEventListener("change", () => fillWeekMonths());
  $("week-month").addEventListener("change", renderWeekList);
  $("week-cancel").addEventListener("click", () => $("week-dialog").close());
  $("tl-next").addEventListener("click", () => { if (weekOffset > 0) { weekOffset -= 1; loadHistory(); } });
}

registerView("history", { load: loadHistory, tick: loadHistory, bindCommon: bindHistoryCommon, ownsDisk: true });
