"use strict";
// 파일 관리자 화면. 뼈대(app.js)의 공통 함수를 쓰고 registerView로 자기 화면을 등록한다.

// 파일 관리자 요청의 필드 이름. 서버의 files.FIELDS와 같아야 한다(시험이 확인한다)
const FILE_FIELDS = {"list": ["root", "path"], "download": ["root", "path"], "mkdir": ["root", "path", "name"], "rename": ["root", "path", "new_name"], "move": ["root", "paths", "dest_root", "dest_path"], "delete": ["root", "paths", "mode"]};

// ---- 파일 관리자 (FM) ----
let fmInfo = null;
let fmRoot = null;
let fmPath = "";
let fmEntries = [];

function fileBody(op, values) {
  const body = {};
  for (const key of FILE_FIELDS[op]) body[key] = values[key];
  return body;
}

async function loadFiles() {
  const info = await act(() => api("GET", "/api/files/roots"));
  if (!info) { $("fm-body").replaceChildren(); $("files-note").textContent = "파일 관리자를 쓸 수 없습니다(설정에서 켜십시오)"; return; }
  fmInfo = info;
  const select = $("fm-root");
  select.replaceChildren(...info.roots.map((r) => el("option", { value: r.key, text: r.label })));
  if (!info.roots.length) { $("files-note").textContent = "허용 루트가 없습니다"; $("fm-body").replaceChildren(); return; }
  if (!info.roots.some((r) => r.key === fmRoot)) { fmRoot = info.roots[0].key; fmPath = ""; }
  select.value = fmRoot;
  $("files-note").textContent = info.readonly ? "읽기 전용" : "";
  $("fm-delete-mode").textContent = info.delete_mode === "trash" ? "삭제하면 휴지통(.trash)으로 옮깁니다" : "삭제하면 완전히 지웁니다";
  for (const b of document.querySelectorAll("[data-write]")) b.hidden = info.readonly;
  listFiles();
}

async function listFiles() {
  const root = fmInfo.roots.find((r) => r.key === fmRoot);
  $("fm-usage").textContent = root && root.usage ? `여유 ${bytes(root.usage.free)} / 전체 ${bytes(root.usage.total)}` : "";
  const q = new URLSearchParams(fileBody("list", { root: fmRoot, path: fmPath }));
  const data = await act(() => api("GET", `/api/files/list?${q}`));
  if (!data) return;
  fmPath = data.path;
  fmEntries = data.entries;
  const crumbs = [el("button", { text: root ? root.label : "/", onclick: () => { fmPath = ""; listFiles(); } })];
  let acc = "";
  for (const part of fmPath ? fmPath.split("/") : []) {
    acc = acc ? `${acc}/${part}` : part;
    const target = acc;
    crumbs.push(el("span", { text: "/" }), el("button", { text: part, onclick: () => { fmPath = target; listFiles(); } }));
  }
  $("fm-crumbs").replaceChildren(...crumbs);
  $("fm-all").checked = false;
  $("fm-body").replaceChildren(...(fmEntries.length ? fmEntries.map((e) => {
    const box = el("input", { type: "checkbox", "aria-label": `${e.name} 선택`, dataset: { path: e.path } });
    const name = e.type === "dir"
      ? el("button", { class: "linklike", text: `${e.name}/`, onclick: () => { fmPath = e.path; listFiles(); } })
      : el("span", { text: e.name });
    const dl = e.type === "file"
      ? el("a", { class: "btn small ghost", text: "내려받기",
        href: `/api/files/download?${new URLSearchParams(fileBody("download", { root: fmRoot, path: e.path }))}` }) : null;
    return el("tr", { dataset: { entry: e.name } }, el("td", {}, box),
      el("td", { class: "file" }, name, e.symlink ? el("span", { class: "muted", text: " (링크)" }) : null,
        e.locked ? el("span", { class: "lock", text: " 잠김" }) : null),
      el("td", { class: "num", text: e.type === "file" ? bytes(e.size) : "" }),
      el("td", { class: "num", text: formatWhen(e.mtime * 1000) }),
      el("td", {}, dl));
  }) : [el("tr", {}, el("td", { colspan: "5", class: "muted", text: "비어 있습니다." }))]));
}

function selectedPaths() {
  return Array.from($("fm-body").querySelectorAll("input[type=checkbox]:checked"), (b) => b.dataset.path);
}

async function fileOp(op, values, ok) {
  const res = await act(() => api("POST", `/api/files/${op}`, fileBody(op, values)), ok);
  if (res) listFiles();
}

function bindFiles() {
  $("fm-root").addEventListener("change", (e) => { fmRoot = e.target.value; fmPath = ""; listFiles(); });
  $("fm-up").addEventListener("click", () => { fmPath = fmPath.split("/").slice(0, -1).join("/"); listFiles(); });
  $("fm-all").addEventListener("change", (e) => {
    for (const b of $("fm-body").querySelectorAll("input[type=checkbox]")) b.checked = e.target.checked;
  });
  $("fm-mkdir").addEventListener("click", () => {
    const name = prompt("새 폴더 이름");
    if (name) fileOp("mkdir", { root: fmRoot, path: fmPath, name }, "폴더를 만들었습니다");
  });
  $("fm-rename").addEventListener("click", () => {
    const paths = selectedPaths();
    if (paths.length !== 1) { toast("하나만 고르십시오", true); return; }
    const current = paths[0].split("/").pop();
    const name = prompt("새 이름", current);
    if (name && name !== current) fileOp("rename", { root: fmRoot, path: paths[0], new_name: name }, "이름을 바꿨습니다");
  });
  $("fm-move").addEventListener("click", () => {
    const paths = selectedPaths();
    if (!paths.length) { toast("옮길 항목을 고르십시오", true); return; }
    const labels = fmInfo.roots.map((r, i) => `${i + 1}. ${r.label}`).join("\n");
    const which = prompt(`옮길 허용 루트 번호\n${labels}`, String(fmInfo.roots.findIndex((r) => r.key === fmRoot) + 1));
    if (!which) return;
    const dest = fmInfo.roots[Number(which) - 1];
    if (!dest) { toast("번호가 틀립니다", true); return; }
    const destPath = prompt("그 루트 안의 폴더(비우면 루트)", dest.key === fmRoot ? fmPath : "");
    if (destPath === null) return;
    fileOp("move", { root: fmRoot, paths, dest_root: dest.key, dest_path: destPath.trim() }, (r) => `${r.moved}개를 옮겼습니다`);
  });
  $("fm-delete").addEventListener("click", () => {
    const paths = selectedPaths();
    if (!paths.length) { toast("지울 항목을 고르십시오", true); return; }
    const mode = fmInfo.delete_mode; // 화면이 알려 준 방식을 그대로 서버에 보낸다
    const how = mode === "trash" ? "휴지통(.trash)으로 옮길까요?" : "완전히 지울까요? 되돌릴 수 없습니다.";
    if (!confirm(`${paths.length}개 항목을 ${how}\n\n${paths.join("\n")}`)) return;
    fileOp("delete", { root: fmRoot, paths, mode }, (r) => `${r.deleted}개를 ${mode === "trash" ? "휴지통으로 옮겼습니다" : "지웠습니다"}`);
  });
}

registerView("files", { load: loadFiles, bind: bindFiles });
