"use strict";
// 색 구성: 기본은 브라우저 설정을 따른다. 화면에서 고른 값은 이 브라우저에만 저장한다.
// 그림이 그려지기 전에 적용하려고 <head>에서 바로 읽는다.
(function () {
  try {
    const saved = localStorage.getItem("lr-theme");
    if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
  } catch (e) { /* 저장소를 못 쓰면 브라우저 설정을 따른다 */ }
})();
