"use strict";
// 쿠키 화면. 플랫폼별 자격. 뼈대(app.js)의 공통 함수를 쓰고 registerView로 자기 화면을 등록한다.

// ---- 쿠키 (플랫폼별 자격. 플랫폼이 하나면 예전과 같게 보인다) ----
let cookiePlatform = null;

function cookieQuery() {
  return cookiePlatform && cookiePlatform !== defaultPlatform ? `?platform=${encodeURIComponent(cookiePlatform)}` : "";
}

function buildCookieForm() {
  const p = platformOf(cookiePlatform);
  if (!p) return;
  const spec = p.credentials;
  $("cookie-title").textContent = spec.title;
  $("cookie-help").textContent = spec.help;
  $("cookie-platform-field").hidden = usablePlatforms().length < 2; // 꺼진 플랫폼의 쿠키 묶음은 감춘다
  if (spec.kind === "file") {
    // 쿠키 파일(cookies.txt) 글 전체를 붙여 넣는다. 화면에는 값 대신 저장 시각·쿠키 수·로그인 쿠키 만료일만
    const row = (key, label) => el("div", { class: "kv" }, el("span", { text: label }), el("span", { dataset: { summary: key } }));
    $("cookie-badges").replaceChildren(
      el("div", { class: "kv" }, el("span", { text: "쿠키 파일" }), el("span", { class: "badge", dataset: { cookie: spec.names[0] } })),
      row("saved_at", "저장한 시각"), row("count", "쿠키 수"), row("login_expires", "로그인 쿠키 만료"));
    $("cookie-fields").replaceChildren(el("label", {}, "cookies.txt 글 전체",
      el("textarea", { name: spec.names[0], autocomplete: "off", spellcheck: "false",
        placeholder: "브라우저에서 내보낸 Netscape 꼴 쿠키 파일의 글 전체를 붙여 넣으십시오" })));
  } else {
    $("cookie-badges").replaceChildren(...spec.names.map((name) =>
      el("div", { class: "kv" }, el("span", { text: name }), el("span", { class: "badge", dataset: { cookie: name } }))));
    if (spec.summary) {
      // 값 대신 저장 시각과 만료 예상일
      for (const [key, label] of [["saved_at", "저장한 시각"], ["expires", "만료 예상일"]]) {
        $("cookie-badges").append(el("div", { class: "kv" }, el("span", { text: label }), el("span", { dataset: { summary: key } })));
      }
    }
    $("cookie-fields").replaceChildren(...spec.names.map((name) =>
      el("label", {}, name, el("input", { name, type: "password", autocomplete: "off", placeholder: "바꿀 때만 입력" }))));
  }
  $("cookie-check").hidden = !spec.checkable;
  $("cookie-last").hidden = !spec.checkable;
  // 유튜브처럼 쿠키 파일 글 전체를 쓰는 플랫폼은 그 플랫폼의 안내(시크릿 창 절차)를 따른다
  $("cookie-upload-extra").textContent = spec.kind === "file" ? "이 플랫폼은 위 안내의 절차대로 내보낸 파일을 올리십시오." : "";
  $("cookie-upload-result").textContent = "";
}

function showCookies(state) {
  const last = state.last_check;
  $("cookie-last").textContent = last ? `마지막 확인 ${formatWhen(last.at, { seconds: true })} · ${last.label}` : "아직 확인하지 않았습니다";
  for (const node of document.querySelectorAll("#cookie-badges [data-cookie]")) setBadge(node, state[node.dataset.cookie], "설정됨", "없음");
  const summary = state.summary || {};
  for (const node of document.querySelectorAll("#cookie-badges [data-summary]")) {
    const key = node.dataset.summary;
    const value = summary[key];
    if (key === "count") node.textContent = value != null ? `${value}개` : "—";
    else if (key === "login_expires") node.textContent = value ? formatWhen(value, { time: false }) : (state.summary ? "브라우저를 닫으면 끝나는 쿠키" : "—");
    else if (key === "expires") node.textContent = value ? formatWhen(value, { time: false }) : (summary.expires_note || "—");
    else node.textContent = value ? formatWhen(value, { seconds: true }) : "—";
  }
}

async function loadCookies() {
  const state = await act(() => api("GET", "/api/cookies" + cookieQuery()));
  if (state) showCookies(state);
}

function bindCookies() {
  cookiePlatform = defaultPlatform;
  $("cookie-platform").replaceChildren(...usablePlatforms().map((p) => el("option", { value: p.id, text: p.name })));
  buildCookieForm();
  $("cookie-platform").addEventListener("change", (event) => { cookiePlatform = event.target.value; buildCookieForm(); loadCookies(); });
  $("cookie-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const body = {};
    for (const input of $("cookie-fields").querySelectorAll("input, textarea")) if (input.value.trim()) body[input.name] = input.value.trim();
    if (!Object.keys(body).length) { toast("바꿀 값을 입력하십시오", true); return; }
    const state = await act(() => api("PUT", "/api/cookies" + cookieQuery(), body), "쿠키를 저장했습니다");
    form.reset();
    if (state) showCookies(state);
  });
  $("cookie-upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = $("cookie-upload-file").files[0];
    let text = $("cookie-upload-text").value;
    if (file) {
      if (file.size > 256 * 1024) { toast("쿠키 파일이 너무 큽니다(256KB 이하만 받습니다)", true); return; }
      text = await file.text();
    }
    if (!text.trim()) { toast("쿠키 파일을 고르거나 내용을 붙여 넣으십시오", true); return; }
    const res = await act(() => api("POST", "/api/cookies/upload" + cookieQuery(), { text }), "쿠키 파일에서 필요한 쿠키를 저장했습니다");
    event.target.reset();
    text = "";
    if (!res) return;
    // 값 없이 결과만: 찾은 쿠키 이름, 저장 시각, 파일에 적힌 만료일
    $("cookie-upload-result").textContent = [`찾은 쿠키: ${res.found.join(", ") || "—"}`, `저장 ${formatWhen(res.saved_at, { seconds: true })}`,
      res.expires ? `만료 ${formatWhen(res.expires, { time: false })}` : "만료일 없음"].join(" · ");
    showCookies(res.state);
  });
  $("cookie-check").addEventListener("click", async () => {
    $("cookie-result").textContent = "확인 중…";
    const result = await act(() => api("POST", "/api/cookies/check" + cookieQuery()));
    $("cookie-result").textContent = result ? `확인 결과: ${result.label}` : "";
    if (result) loadCookies();
  });
  $("cookie-clear").addEventListener("click", async () => {
    if (!confirm("저장한 쿠키를 지울까요?")) return;
    const state = await act(() => api("DELETE", "/api/cookies" + cookieQuery()), "쿠키를 지웠습니다");
    if (state) showCookies(state);
  });
}

registerView("cookies", { load: loadCookies, bind: bindCookies });
