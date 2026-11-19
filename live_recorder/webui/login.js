"use strict";
let form = null;
let errorBox = null;
let forgotHint = null;

// 비밀번호 분실 안내는 이 브라우저(탭)에서 로그인을 3번 틀린 뒤에만 보인다. 성공하면 횟수를 지운다.
// 횟수는 화면 표시용이다. 실제 잠금은 서버의 로그인 실패 제한이 한다
const FORGOT_AFTER = 3;
const FAIL_KEY = "lr-login-failures";
let memoryFailures = 0;

function readFailures() {
  try { return Number(sessionStorage.getItem(FAIL_KEY)) || 0; } catch (e) { return memoryFailures; }
}

function writeFailures(n) {
  memoryFailures = n;
  try {
    if (n > 0) sessionStorage.setItem(FAIL_KEY, String(n));
    else sessionStorage.removeItem(FAIL_KEY);
  } catch (e) { /* 저장소를 못 쓰면 이 화면 안에서만 센다 */ }
}

function showForgotHint() {
  forgotHint.hidden = readFailures() < FORGOT_AFTER;
}

async function submitLogin(event) {
  event.preventDefault();
  errorBox.hidden = true;
  const body = { username: form.username.value, password: form.password.value };
  try {
    const resp = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin", body: JSON.stringify(body),
    });
    if (resp.ok) { writeFailures(0); location.replace("/"); return; }
    if (resp.status === 401) writeFailures(readFailures() + 1); // 틀린 사용자 이름·비밀번호만 센다(잠금·연결 오류는 세지 않는다)
    const data = await resp.json().catch(() => ({}));
    errorBox.textContent = data.error || "로그인하지 못했습니다";
  } catch (e) {
    errorBox.textContent = "서버에 연결하지 못했습니다";
  }
  errorBox.hidden = false;
  showForgotHint();
  form.password.value = "";
}

function start() {
  form = document.getElementById("login-form");
  errorBox = document.getElementById("login-error");
  forgotHint = document.getElementById("forgot-hint");
  showForgotHint();
  form.addEventListener("submit", submitLogin);
}

// 시작이 실패하면 boot.js가 고정 안내를 보인다
if (window.lrBoot) window.lrBoot.run(start);
else start();
