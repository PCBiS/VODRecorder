"use strict";
// 첫 설정. 서버에서 온 문자열은 textContent로만 넣는다
let form = null;
let errorBox = null;
let rule = null; // 서버의 비밀번호 규칙. 숫자를 이 파일에 적지 않는다
let useCode = true; // 서버가 설정 코드를 끈 상태면 코드 칸을 감춘다

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

async function submitSetup(event) {
  event.preventDefault();
  errorBox.hidden = true;
  const body = {
    username: form.username.value.trim(),
    password: form.password.value,
    password_confirm: form.password_confirm.value,
  };
  if (useCode) body.code = form.code.value.trim(); // 코드를 끈 서버에는 보내지 않는다
  if (rule && (body.password.length < rule.min || body.password.length > rule.max)) { showError(rule.text); return; }
  if (body.password !== body.password_confirm) { showError("비밀번호가 서로 다릅니다"); return; }
  try {
    const resp = await fetch("/api/setup", {
      method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin", body: JSON.stringify(body),
    });
    if (resp.ok) { location.replace("/"); return; }
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 409 || resp.status === 401) { location.replace("/login"); return; } // 이미 계정이 있다
    showError(data.error || "계정을 만들지 못했습니다");
  } catch (e) {
    showError("서버에 연결하지 못했습니다");
  }
  form.password.value = "";
  form.password_confirm.value = "";
}

async function start() {
  form = document.getElementById("setup-form");
  errorBox = document.getElementById("setup-error");
  form.addEventListener("submit", submitSetup);
  // 규칙을 받지 못하면 규칙 문구를 보일 수 없으므로 시작 실패로 본다
  const resp = await fetch("/api/mode", { credentials: "same-origin" });
  if (!resp.ok) throw new Error(`/api/mode ${resp.status}`);
  const mode = await resp.json();
  rule = mode.password;
  if (mode.setup_code === false) {
    useCode = false;
    for (const node of document.querySelectorAll("[data-code-part]")) node.hidden = true;
    form.code.required = false;
    const warn = document.getElementById("setup-warning");
    warn.textContent = mode.setup_warning || "";
    warn.hidden = !mode.setup_warning;
  }
  for (const node of document.querySelectorAll("[data-pw-rule]")) node.textContent = rule.text;
  for (const input of document.querySelectorAll("[data-pw-input]")) { input.minLength = rule.min; input.maxLength = rule.max; }
}

// 시작이 실패하면 boot.js가 고정 안내를 보인다
if (window.lrBoot) window.lrBoot.run(start);
else start().catch((e) => console.error("화면을 시작하지 못했다", e));
