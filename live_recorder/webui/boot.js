"use strict";
// 화면 시작 코드가 실패하면(동기 예외, 거부된 Promise, 스크립트가 아예 돌지 않음) 본문 자리에 고정 안내와
// 다시 불러오기 버튼을 보인다. 오류 내용은 화면에 쓰지 않고 브라우저 콘솔에만 남긴다.
// 각 화면의 스크립트보다 먼저 읽고, 화면 스크립트는 시작 코드를 `lrBoot.run(start)`로 돌린다.
(function () {
  let started = false; // 화면 스크립트가 시작 코드를 넘겼다
  let settled = false; // 시작이 끝났다(성공이든 실패든). 그 뒤의 오류는 이 안내와 무관하다
  let skipped = false; // 다른 화면으로 옮겨 가는 중이라 안내를 띄우지 않는다

  function showPanel() {
    for (const node of document.querySelectorAll("[data-boot-hide]")) node.hidden = true;
    const panel = document.getElementById("boot-failed");
    if (!panel) return;
    panel.hidden = false;
    const retry = document.getElementById("boot-retry");
    if (retry) retry.focus();
  }

  function stopListening() {
    window.removeEventListener("error", onError);
    window.removeEventListener("unhandledrejection", onRejection);
  }

  function fail(err) {
    if (settled) return;
    settled = true;
    stopListening();
    console.error("화면을 시작하지 못했다", err);
    if (skipped) return;
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", showPanel);
    else showPanel();
  }

  function done() {
    if (settled) return;
    settled = true;
    stopListening();
  }

  function onError(event) { fail(event.error || event.message); }
  function onRejection(event) { fail(event.reason); }

  window.addEventListener("error", onError);
  window.addEventListener("unhandledrejection", onRejection);

  function bindRetry() {
    const retry = document.getElementById("boot-retry");
    if (retry) retry.addEventListener("click", () => location.reload());
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bindRetry);
  else bindRetry();

  // 화면 스크립트를 받지 못했거나(404 등) 시작 코드를 넘기지 않은 채 문서를 다 읽었다
  window.addEventListener("load", () => { if (!started) fail(new Error("화면 스크립트가 시작하지 않았습니다")); });

  window.lrBoot = {
    run(start) {
      started = true;
      let result;
      try {
        result = start();
      } catch (err) {
        fail(err);
        return Promise.resolve();
      }
      return Promise.resolve(result).then(done, fail);
    },
    // 로그인 화면으로 보내는 등 페이지를 떠나는 중이면 실패 안내를 띄우지 않는다(콘솔 기록은 남긴다)
    leaving() { skipped = true; },
  };
})();
