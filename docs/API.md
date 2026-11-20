# VOD Recorder API 안내

화면에서 하는 일 가운데 상태 보기와 녹화·다운로드 운영을 프로그램(스크립트, AI 도구 등)이 API로 할 수 있습니다. 이 글은 서버의 `GET /api/guide`에서도 같은 내용으로 받을 수 있습니다. 기계가 읽는 문서(OpenAPI)는 `GET /api/openapi.json`에 있습니다.

## 인증

1. 화면의 설정 > 계정·보안 > "접근 키"에서 키를 발급합니다. 이름, 권한(보기·운영), 만료(없음·30일·90일·1년)를 정합니다.
2. 키 값(`vr_`로 시작)은 발급한 그때 한 번만 보입니다. 서버는 값을 저장하지 않고 해시만 두므로 다시 볼 수 없습니다. 잃어버리면 폐기하고 새로 발급하십시오.
3. 요청마다 머리글 `Authorization: Bearer <키>`를 붙입니다. 이 머리글이 있으면 로그인 세션과 CSRF 토큰을 보지 않고, 응답에 쿠키를 심지 않습니다.
4. 키가 없거나 틀리거나 만료·폐기됐으면 401입니다(까닭은 구분해 알리지 않습니다). 틀린 키를 여러 번 보내면 그 주소에서 오는 시도를 잠시 막습니다(로그인 실패 제한과 같은 규칙, 10분에 5번, IPv6는 /64 묶음 단위). 맞는 키는 세지 않으므로 같은 키로 동시에 여러 요청을 보내도 됩니다(요청 수 제한은 아래 표를 따릅니다).
5. 키는 최대 10개까지 만들 수 있고, 화면에서 바로 폐기할 수 있습니다(폐기는 곧바로 먹습니다).
   비밀번호를 바꿀 때 "접근 키도 모두 폐기"를 고르면 모든 키가 함께 폐기됩니다(기본은 그대로 둡니다). 명령줄로 관리자 계정을 지우면 모든 키를 폐기합니다. 관리자 계정이 없는 동안에는 남아 있는 키도 받지 않습니다(401).
   `Authorization` 머리글이 `Bearer`가 아니면(예: `Basic`) 키 요청으로 보지 않습니다.

키를 가진 프로그램은 화면 없이 녹화를 시작·중지하고 채널을 지울 수 있습니다(운영 권한). 키 값은 비밀번호처럼 다루십시오.

## 권한

- **보기**: 상태, 채널, 세션 기록, 다운로드 작업, 수집 목록, 파일 목록과 파일 내려받기, 플랫폼과 확장의 상태, API 문서를 읽습니다.
- **운영**: 보기에 더해 채널 추가·수정·삭제, 녹화 시작·중지와 지금 확인, 다운로드의 분석·작업 추가·일시정지·재개·취소·재시도, 다시보기 목록 수집을 합니다.

경로마다 키로 열 수 있는지와 필요한 권한이 정해져 있습니다. 아래 표에 없는 경로는 키로 부르면 403입니다. 다운로드(`/api/replay/…`) 경로는 그 기능의 확장이 켜져 있을 때만 있습니다(꺼져 있으면 404).

| 방법 | 경로 | 권한 |
|---|---|---|
| GET | /status | 보기 |
| GET | /api/mode | 보기 |
| GET | /api/channels | 보기 |
| GET | /api/view/channels | 보기 |
| GET | /api/sessions | 보기 |
| GET | /api/view/sessions | 보기 |
| GET | /api/platforms | 보기 |
| GET | /api/extensions | 보기 |
| GET | /api/files/roots | 보기 |
| GET | /api/files/list | 보기 |
| GET | /api/files/download | 보기 |
| GET | /api/openapi.json | 보기 |
| GET | /api/guide | 보기 |
| POST | /api/channels/resolve | 운영 |
| POST | /api/channels | 운영 |
| PATCH | /api/channels/{channel_id} | 운영 |
| DELETE | /api/channels/{channel_id} | 운영 |
| POST | /api/channels/{channel_id}/start | 운영 |
| POST | /api/channels/{channel_id}/stop | 운영 |
| GET | /api/replay/kinds | 보기 |
| GET | /api/replay/status | 보기 |
| GET | /api/replay/analyses/{ident} | 보기 |
| GET | /api/replay/jobs | 보기 |
| GET | /api/replay/jobs/{ident} | 보기 |
| GET | /api/replay/jobs/{ident}/logs | 보기 |
| GET | /api/replay/jobs/{ident}/file | 보기 |
| PUT | /api/replay/control | 운영 |
| POST | /api/replay/cancel-active | 운영 |
| POST | /api/replay/analyze | 운영 |
| POST | /api/replay/jobs | 운영 |
| POST | /api/replay/jobs/{ident}/cancel | 운영 |
| POST | /api/replay/jobs/{ident}/retry | 운영 |
| POST | /api/replay/jobs/{ident}/pause | 운영 |
| POST | /api/replay/jobs/{ident}/resume | 운영 |
| GET | /api/replay/channels | 보기 |
| GET | /api/replay/channels/{channel_id}/months | 보기 |
| GET | /api/replay/channels/{channel_id}/videos | 보기 |
| POST | /api/replay/channels | 운영 |
| POST | /api/replay/channels/{channel_id}/collect | 운영 |
| POST | /api/replay/channels/{channel_id}/collect/cancel | 운영 |
| DELETE | /api/replay/channels/{channel_id} | 운영 |
| POST | /api/replay/select | 운영 |

채널은 플랫폼과 채널 ID로 가리킵니다. 치지직이 아닌 채널은 주소 끝에 `?platform=youtube`처럼 플랫폼을 붙입니다.

## 키로 할 수 없는 일

사람이 화면에서만 합니다: 설정 변경과 설정 읽기, 쿠키 입력·삭제·파일 올리기, 플랫폼과 확장 켜고 끄기, 플랫폼별 확인 간격, 파일의 이름 변경·이동·삭제·폴더 만들기, 계정과 비밀번호, 접근 키 관리, 로그 보기와 내려받기, 외부 도구(yt-dlp) 갱신·되돌리기와 도구 상태, 텔레그램 설정과 테스트 전송.

키로 받는 응답에는 쿠키, 텔레그램 토큰, 접근 키, 설정 코드 같은 비밀값이 들어 있지 않습니다.

## 자주 하는 일

### 채널 추가 → 상태 보기 → 녹화 중지

```sh
KEY=vr_…   # 발급한 운영 키
BASE=http://127.0.0.1:8080

# 채널 칸의 글(주소나 @핸들)을 채널 ID로 바꿉니다(플랫폼에 한 번 묻습니다)
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"text": "https://chzzk.naver.com/live/<채널ID>"}' $BASE/api/channels/resolve

# 채널을 더합니다. output_dir(녹화 폴더 아래의 저장 폴더)은 생략할 수 있습니다.
# 생략하거나 비우면 <플랫폼 식별자>/<채널 이름>(예: chzzk/채널 이름)에 저장합니다. 이름의 파일 이름에 못 쓰는 글자는 _로 바뀌고,
# 같은 폴더를 다른 채널이 쓰고 있으면 끝에 _<채널 ID 앞 8자>가 붙습니다. 폴더는 더할 때 한 번 정해지고 이름을 바꿔도 그대로입니다
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"channel_id": "<채널ID>", "name": "채널 이름"}' $BASE/api/channels

# 상태를 봅니다(state: watching·recording·backoff·stopped 등, error: 지금 남아 있는 문제)
curl -s -H "Authorization: Bearer $KEY" $BASE/api/channels

# 지금 확인(방송 중이면 녹화를 시작합니다)과 녹화 중지
curl -s -X POST -H "Authorization: Bearer $KEY" $BASE/api/channels/<채널ID>/start
curl -s -X POST -H "Authorization: Bearer $KEY" $BASE/api/channels/<채널ID>/stop
```

### 주소로 받기 → 진행 보기

```sh
# 주소를 분석합니다(한 번에 10개까지). 분석은 기다려야 끝납니다
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"urls": ["https://chzzk.naver.com/video/<번호>"]}' $BASE/api/replay/analyze
# → {"id": "<분석 ID>", ...}
curl -s -H "Authorization: Bearer $KEY" $BASE/api/replay/analyses/<분석 ID>
# status가 completed가 되면 items[].qualities[].id 가운데 하나를 골라 작업을 더합니다.
# key는 같은 요청을 두 번 보내도 한 번만 더하게 하는 임의의 글입니다
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"analysis_id": "<분석 ID>", "items": [{"url": "<items[].url>", "quality": "<화질 ID>"}], "key": "my-request-0001"}' \
  $BASE/api/replay/jobs
# 진행: status(queued·running·paused·completed·failed), progress(백분율)
curl -s -H "Authorization: Bearer $KEY" $BASE/api/replay/jobs/<작업 ID>
```

## 오류와 요청 제한

키로 부른 요청의 오류는 모두 같은 꼴의 JSON입니다.

```json
{"detail": "사람이 읽는 문장", "code": "짧은 이름"}
```

| 상태 | code | 뜻 |
|---|---|---|
| 401 | unauthorized | 키가 없거나 틀리거나 만료·폐기됨 |
| 403 | forbidden | 키의 권한이 모자라거나 키로 열 수 없는 경로 |
| 404 | not_found | 없는 채널·작업·경로, 꺼진 확장의 경로 |
| 409 | conflict | 지금은 할 수 없음(녹화 중, 꺼진 플랫폼 등) |
| 422 | invalid_input | 입력이 맞지 않음(`errors`에 칸별 까닭이 붙을 수 있습니다) |
| 413 | too_large | 요청 본문이 너무 큼(기본 1 MiB) |
| 429 | rate_limited | 요청 제한을 넘음. `Retry-After` 머리글의 초만큼 기다린 뒤 다시 보내십시오 |

요청 제한은 키마다 1분에 읽기 60번, 바꾸는 요청 20번입니다. 플랫폼으로 요청이 나가는 동작(지금 확인, 분석, 채널 조회)은 서버의 플랫폼 요청 간격을 그대로 따릅니다. 키로 불러도 그 간격보다 빨리 나가지 않으므로 응답이 늦어질 수 있습니다.

## 기록

키로 한 바꾸는 요청은 서버 로그에 키 이름과 값의 앞 8자, 방법, 경로의 꼴, 결과 코드가 남습니다(본문과 키 값은 남기지 않습니다). 읽기 요청은 키의 마지막 사용 시각과 횟수만 바뀝니다. 화면의 접근 키 목록에서 볼 수 있습니다.
