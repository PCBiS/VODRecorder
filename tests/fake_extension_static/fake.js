"use strict";
// 가짜 확장의 화면. 뼈대의 registerView·el·api만 쓴다
async function loadFakeExt() {
  const data = await act(() => api("GET", "/api/fakeext/ping"));
  $("view-fakeext").replaceChildren(el("div", { class: "panel" }, el("h1", { text: "가짜 확장" }),
    el("p", { id: "fakeext-result", text: data ? `단계 ${data.level}, 사건 ${data.events}개입니다` : "" })));
}

function bindFakeExt() {
  const group = $("ext-group-fakeext");
  if (group) group.append(el("p", { id: "fakeext-group-text", class: "muted", text: "가짜 확장 설정 묶음입니다" }));
}

registerView("fakeext", { load: loadFakeExt, bind: bindFakeExt });
