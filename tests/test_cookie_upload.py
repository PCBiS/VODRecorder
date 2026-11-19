"""쿠키 파일 올리기. 두 꼴(Netscape cookies.txt, JSON 배열)의 해석과 플랫폼별 골라 담기."""

import json
import time

import pytest

from live_recorder.platforms import cookiefile, credentials, registry

FUTURE = int(time.time()) + 30 * 86400
LATER = FUTURE + 86400
PAST = int(time.time()) - 86400

NETSCAPE = "\n".join([
    "# Netscape HTTP Cookie File",
    f".naver.com\tTRUE\t/\tTRUE\t{FUTURE}\tNID_AUT\taut-secret-0001",
    f"#HttpOnly_.naver.com\tTRUE\t/\tTRUE\t{LATER}\tNID_SES\tses-secret-0002",
    f"nid.naver.com\tFALSE\t/\tTRUE\t{LATER}\tNID_AUT\taut-subdomain-0003",
    f".example.org\tTRUE\t/\tFALSE\t{FUTURE}\tSESSION\tother-site-secret-0004",
    f".ci.me\tTRUE\t/\tTRUE\t{FUTURE}\tmauth-authorization-code\teyJ.cime-secret.0005",
    f".youtube.com\tTRUE\t/\tTRUE\t{FUTURE}\tSID\tyt-sid-0006",
    f".youtube.com\tTRUE\t/\tTRUE\t{FUTURE}\tSAPISID\tyt-sapisid-0007",
    f".google.com\tTRUE\t/\tTRUE\t{PAST}\tNID\tgoogle-expired-0008",
]) + "\n"

JSON_FILE = json.dumps([
    {"domain": ".naver.com", "name": "NID_AUT", "value": "aut-json-0011", "expirationDate": FUTURE + 0.5, "path": "/"},
    {"domain": ".naver.com", "name": "NID_SES", "value": "ses-json-0012", "session": True},
    {"domain": "www.example.org", "name": "x", "value": "other-json-0013", "expirationDate": FUTURE},
])


def test_parse_both_shapes_and_errors():
    a = cookiefile.parse(NETSCAPE)
    assert len(a) == 8 and any(c.http_only and c.name == "NID_SES" for c in a)
    b = cookiefile.parse(JSON_FILE)
    assert [c.name for c in b] == ["NID_AUT", "NID_SES", "x"] and b[1].expires is None
    for bad, why in (("", "비었습니다"), ("hello world", "알아보지 못했습니다"), ("[1, 2]", "domain·name·value"),
                     ("[broken", "읽지 못했습니다"), (".a\tTRUE\t/\n", "꼴이 맞지 않습니다"), ("x\t" * 200000, "너무 큽니다")):
        with pytest.raises(ValueError, match=why):
            cookiefile.parse(bad)
    with pytest.raises(ValueError) as e:
        cookiefile.parse(".a\tTRUE\t/\tTRUE\t0\tN\tsecret-in-file-9999\textra\n")
    assert "secret-in-file-9999" not in str(e.value)  # 오류 문장에 내용이 없다


def test_pick_rules():
    chzzk = registry.get("chzzk")
    values, info = chzzk.pick_credentials(NETSCAPE)
    # 같은 이름이 여럿이면 도메인이 더 맞는 것(.naver.com)을 쓴다(하위 도메인 nid.naver.com의 값이 더 늦게 끝나도)
    assert values == {"NID_AUT": "aut-secret-0001", "NID_SES": "ses-secret-0002"} and info["names"] == ["NID_AUT", "NID_SES"]
    assert info["expires"] is not None
    values, _ = chzzk.pick_credentials(JSON_FILE)
    assert values == {"NID_AUT": "aut-json-0011", "NID_SES": "ses-json-0012"}
    with pytest.raises(ValueError, match="NID_SES 쿠키가 없습니다. 로그인한 상태에서"):
        chzzk.pick_credentials(f".naver.com\tTRUE\t/\tTRUE\t{FUTURE}\tNID_AUT\tv\n")
    with pytest.raises(ValueError, match="이미 만료됐습니다"):
        chzzk.pick_credentials(f".naver.com\tTRUE\t/\tTRUE\t{PAST}\tNID_AUT\tv\n.naver.com\tTRUE\t/\tTRUE\t{PAST}\tNID_SES\tv\n")
    values, info = registry.get("cime").pick_credentials(NETSCAPE)
    assert values == {"mauth-authorization-code": "eyJ.cime-secret.0005"}
    values, info = registry.get("youtube").pick_credentials(NETSCAPE)
    text = values["cookies_txt"]
    assert "yt-sid-0006" in text and "other-site" not in text and "naver" not in text and "google-expired" not in text
    assert info["names"] == ["SAPISID", "SID"]


@pytest.fixture
def site(tmp_path, server, tools):
    from test_web import Web

    with Web(tmp_path, server, tools) as w:
        w.login()
        yield w


def test_upload_api(site):
    r = site.call("POST", "/api/cookies/upload", json={"text": NETSCAPE})
    body = r.json()
    assert r.status_code == 200 and body["found"] == ["NID_AUT", "NID_SES"] and body["saved_at"] and body["expires"]
    assert body["state"]["NID_AUT"] is True and "secret" not in r.text
    assert credentials.load(site.h.cfg, "chzzk") == {"NID_AUT": "aut-secret-0001", "NID_SES": "ses-secret-0002"}
    stored = credentials.path(site.h.cfg, "chzzk").read_text(encoding="utf-8")
    assert "other-site-secret-0004" not in stored and "aut-subdomain-0003" not in stored  # 나머지 줄은 버린다
    # 없는 쿠키: 저장하지 않고 무엇이 없는지 알린다(이미 저장한 값은 그대로)
    r = site.call("POST", "/api/cookies/upload", json={"text": f".naver.com\tTRUE\t/\tTRUE\t{FUTURE}\tNID_AUT\tnew-0001\n"})
    assert r.status_code == 400 and "NID_SES 쿠키가 없습니다" in r.text and "new-0001" not in r.text
    assert credentials.load(site.h.cfg, "chzzk")["NID_AUT"] == "aut-secret-0001"
    assert site.call("POST", "/api/cookies/upload", json={"text": "x" * (300 * 1024)}).status_code == 400
    assert site.call("POST", "/api/cookies/upload", json={"text": "not a cookie file"}).status_code == 400
    assert site.client.post("/api/cookies/upload", json={"text": NETSCAPE}).status_code == 403  # CSRF
    # 꺼진 플랫폼은 거부
    r = site.call("POST", "/api/cookies/upload?platform=cime", json={"text": NETSCAPE})
    assert r.status_code == 409
    site.h.store.set_platform_enabled("cime", True)
    r = site.call("POST", "/api/cookies/upload?platform=cime", json={"text": NETSCAPE})
    assert r.status_code == 200 and r.json()["found"] == ["mauth-authorization-code"]
    site.h.store.set_platform_enabled("youtube", True)
    r = site.call("POST", "/api/cookies/upload?platform=youtube", json={"text": JSON_FILE})
    assert r.status_code == 400 and "유튜브·구글 쿠키가 없습니다" in r.text
    assert site.call("POST", "/api/cookies/upload?platform=youtube", json={"text": NETSCAPE}).status_code == 200
    log = site.h.log_text()
    for secret in ("aut-secret-0001", "ses-secret-0002", "other-site-secret-0004", "eyJ.cime-secret.0005", "yt-sid-0006"):
        assert secret not in log
