"""저장 폴더의 기본값. 비워 두면 <녹화 루트>/<플랫폼 식별자>/<채널 이름>."""

import asyncio
import re
from pathlib import Path

import pytest

import fake_cime
from conftest import CHANNEL, Harness
from live_recorder import cli
from live_recorder.core import store as st
from live_recorder.platforms import registry
from live_recorder.platforms.cime import platform as cm

OTHER = "b" * 32


@pytest.fixture
def h(tmp_path, server, tools):
    return Harness(tmp_path, server, tools)


def rel(h, ch):
    return str(Path(ch.output_dir).relative_to(h.root))


def test_empty_folder_becomes_platform_and_channel_name(h):
    ch = h.store.add_channel(CHANNEL, "가나다", "")
    assert rel(h, ch) == "chzzk/가나다"
    ch2 = h.store.add_channel("c" * 32, "다른 이름", None)
    assert rel(h, ch2) == "chzzk/다른 이름"
    yt = h.store.add_channel("UC" + "a" * 22, "유튜브 채널", "  ", platform="youtube")
    assert rel(h, yt) == "youtube/유튜브 채널"
    cimech = h.store.add_channel("1007346", "씨미 채널", "", platform="cime")
    assert rel(h, cimech) == "cime/씨미 채널"
    # 폴더 이름은 플랫폼 모듈의 식별자 그대로다
    assert {rel(h, c).split("/")[0] for c in (ch, yt, cimech)} == {p.id for p in (registry.get("chzzk"), registry.get("youtube"),
                                                                                 registry.get("cime"))}


def test_bad_characters_empty_names_and_collisions(h):
    ch = h.store.add_channel(CHANNEL, ' 이상한/이름: "별*표"? . ', "")
    assert rel(h, ch) == "chzzk/이상한_이름_ _별_표"  # 금지 글자는 _, 양끝의 공백·점·밑줄은 뗀다
    for i, name in enumerate(("...", "./..", "/")):
        cid = f"{i:032x}"
        c = h.store.add_channel(cid, name, "")
        assert rel(h, c) == f"chzzk/{cid}", name  # 비거나 . .. 이면 채널 ID
    # 같은 이름의 다른 채널: 끝에 채널 ID 앞 8자
    h.store.add_channel("d" * 32, "같은이름", "")
    second = h.store.add_channel(OTHER, "같은이름", "")
    assert rel(h, second) == f"chzzk/같은이름_{OTHER[:8]}"
    # 다른 플랫폼이면 폴더가 달라 겹치지 않는다
    yt = h.store.add_channel("UC" + "b" * 22, "같은이름", "", platform="youtube")
    assert rel(h, yt) == "youtube/같은이름"


def test_given_value_kept_existing_and_update_unchanged(h):
    ch = h.store.add_channel(CHANNEL, "가나다", "my/folder")
    assert rel(h, ch) == "my/folder"
    root = h.store.add_channel(OTHER, "루트", ".")
    assert Path(root.output_dir) == h.root.resolve()  # 녹화 루트 자체도 넣을 수 있다
    with pytest.raises(st.ValidationError):
        h.store.add_channel("e" * 32, "밖", "../out")
    # 이름을 바꿔도 폴더는 따라가지 않고, 수정에서 빈 폴더는 "바꾸지 않음"
    auto = h.store.add_channel("f" * 32, "처음 이름", "")
    h.store.update_channel("f" * 32, name="새 이름", output_dir="")
    after = h.store.get_channel("f" * 32)
    assert after.name == "새 이름" and rel(h, after) == "chzzk/처음 이름"
    h.store.update_channel(CHANNEL, output_dir="")
    assert rel(h, h.store.get_channel(CHANNEL)) == "my/folder"


def test_api_and_cli(tmp_path, server, tools, capsys):
    from test_web import Web

    with Web(tmp_path, server, tools) as w:
        w.login()
        r = w.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "가나다"})
        assert r.status_code == 201, r.text
        r = w.call("POST", "/api/channels", json={"channel_id": OTHER, "name": "다른", "output_dir": ""})
        assert r.status_code == 201
        folders = {c["channel_id"]: c["output_dir"] for c in w.client.get("/api/channels").json()["channels"]}
        assert folders == {CHANNEL: "chzzk/가나다", OTHER: "chzzk/다른"}
        r = w.call("PATCH", f"/api/channels/{CHANNEL}", json={"output_dir": ""})
        assert r.status_code == 200
        assert {c["channel_id"]: c["output_dir"] for c in w.client.get("/api/channels").json()["channels"]}[CHANNEL] == "chzzk/가나다"
    h = Harness(tmp_path / "cli", server, tools)
    code = cli.main(["--data-dir", str(h.cfg.data_dir), "--record-root", str(h.root), "channel", "add", "c" * 32, "--name", "명령줄"])
    assert code == 0, capsys.readouterr()
    ch = st.Store(h.cfg.db_path, h.root, h.cfg.telegram_path).get_channel("c" * 32)
    assert str(Path(ch.output_dir).relative_to(h.root)) == "chzzk/명령줄"


def test_screen_hint_and_field_not_required():
    from webui_files import all_js, index_html

    html, js = index_html(), all_js()
    field = re.search(r'<input name="output_dir"[^>]*>', html).group(0)
    assert "required" not in field
    assert "비워 두면 ${p.id}/${name || \"(채널 이름)\"}에 저장합니다" in js and "비워 두면 바꾸지 않습니다" in js
    assert '$("channel-form").name.addEventListener("input", showFolderHint)' in js


def test_screen_hint_computes_platform_and_name():
    import json
    import subprocess

    js = all_js_text()
    body = js[js.index("function showFolderHint"):js.index("async function submitChannel")]
    body = body[:body.index("function openChannelDialog")] if "function openChannelDialog" in body else body
    script = ("let editing = null; const form = { name: { value: '' }, output_dir: { placeholder: '' } };\n"
              "function $() { return form; } function formPlatform() { return 'cime'; }\n"
              "function platformOf(id) { return { id }; }\n" + body +
              "\nconst out = []; form.name.value = ''; showFolderHint(); out.push(form.output_dir.placeholder);"
              "form.name.value = ' 이름/에*별 '; showFolderHint(); out.push(form.output_dir.placeholder);"
              "editing = {}; showFolderHint(); out.push(form.output_dir.placeholder); console.log(JSON.stringify(out));")
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out == ["비워 두면 cime/(채널 이름)에 저장합니다", "비워 두면 cime/이름_에_별에 저장합니다", "비워 두면 바꾸지 않습니다"]


def all_js_text():
    from webui_files import all_js

    return all_js()


def test_migration_keeps_given_folders():
    """이전 도구로 가져오는 채널은 원래 폴더를 그대로 쓴다(빈 값이면 예전처럼 건너뛴다)."""
    from live_recorder import migrate

    src = Path(migrate.__file__).read_text(encoding="utf-8")
    assert "_under_root(record_root, mapper(str(rec.get(\"output_dir\") or \"\")))" in src
    with pytest.raises(st.ValidationError):
        migrate._under_root(Path("/tmp"), "")
