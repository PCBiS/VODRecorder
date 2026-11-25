# 로그 내려받기 묶음에 들어가는 것

## 로그를 보내기 전에 읽어 주십시오

설정 > 일반의 "로그 내려받기"는 문제를 알려 주실 때 원인을 바로 볼 수 있도록, 로그를 거의 그대로 묶습니다. 많이 가리기보다 무엇이 들어가는지 알려 드리는 쪽을 택했습니다.

**들어가는 정보**

- 녹화하는 채널의 이름과 채널 ID, 다시보기 채널의 이름과 ID
- 방송·영상 제목, 녹화 파일 이름과 서버 안의 경로
- 녹화 시각과 길이, 파일 크기, 영상 번호와 방송 번호(liveId)
- 설정값(비밀이 아닌 것), 오류 내용과 오류 문장
- 프로그램·Python·ffmpeg 버전, 운영체제 문자열, 디스크 용량
- 치지직 주소(서명·토큰 값은 가립니다), 유튜브 영상 ID와 주소의 호스트·꼴(서명·만료 등 값은 가립니다)
- yt-dlp 판과 yt-dlp가 낸 경고 문장(`live_recorder.youtube`)
- 접근 키로 한 바꾸는 요청의 기록(`api_key_request`: 키 이름, 키 값의 앞 8자, 방법, 경로의 꼴, 결과 코드)과 키 발급·폐기·실패 사건(`api_key_created`·`api_key_revoked`·`api_key_failed`). 키 값과 요청 본문은 남기지 않습니다

**들어가지 않는 정보**

- 쿠키(`NID_AUT`·`NID_SES`, 씨미 `mauth-authorization-code`와 다시보기·클립의 서명 쿠키 `CloudFront-Policy`·`CloudFront-Signature`·`CloudFront-Key-Pair-Id`, 유튜브 `SID`·`SAPISID`·`__Secure-3PAPISID`·`LOGIN_INFO` 등. 유튜브 쿠키 파일 글 전체), 관리자 비밀번호, 텔레그램 봇 토큰과 chat_id, 첫 설정 코드, 로그인 세션·CSRF 토큰, 접근 키 값(`vr_…`, `Authorization` 머리글). 로그에 섞여 있더라도 `[가림]`으로 바꿉니다.
- 접속한 쪽의 주소(IPv4·IPv6)는 `[주소]`, 관리자 사용자 이름은 `관리자`로 바꿉니다.
- 영상 파일, DB 파일(`recorder.sqlite3`·`replay.sqlite3`), 비밀 저장소 파일(`secrets/`)은 넣지 않습니다.

**올리기 전에**

- 이슈 게시판처럼 공개된 곳에 올리면 누구나 볼 수 있습니다. 위의 "들어가는 정보"가 모두 보입니다.
- 꺼려지면 내려받기 전에 "더 가리기"를 켜십시오. 채널·제목·파일 이름·주소를 대신 이름으로 바꾸고 문장을 뺍니다. 대신 문제 분석이 어려워집니다.
- 또는 올리기 전에 zip을 직접 열어 보십시오. 모두 글자 파일(JSON)이라 메모장으로 열립니다. 아래 "보내기 전에 볼 것"을 참고하십시오.

## 이 문서를 고칠 때

로그에 새 필드나 새 이벤트를 더하면 이 문서의 표와 가리기 규칙(`live_recorder/logmask.py`의 `FIELDS`)을 함께 고칩니다. `tests/test_logexport.py`가 코드의 로그 호출에 쓰인 `extra` 필드 이름과 `event` 값이 모두 이 문서에 있는지 확인합니다.

## 만드는 방식

| 항목 | 내용 |
|---|---|
| 요청 | `POST /api/logs/export`, 본문 `{"range": "1"\|"3"\|"7"\|"all", "strict": true\|false}`. 기본은 최근 3일, 더 가리기 꺼짐(`strict` 생략 = `false`) |
| 권한 | 로그인이 필요합니다. CSRF 검사를 거칩니다. 계정이 없는 보기 전용 모드에서는 거부합니다 |
| 파일 이름 | `vodrecorder-logs-YYYYMMDD-HHMMSS.zip`. 시각은 Asia/Seoul입니다 |
| 서버에 남는 것 | 없습니다. 묶음은 메모리에서 만들어 응답으로만 보냅니다. 임시 파일을 쓰지 않습니다. 로그에 `log_export` 이벤트 한 줄이 남습니다 |
| 동시 요청 | 한 번에 하나만 만듭니다. 만드는 중에 다시 누르면 409로 거부합니다. 묶음은 별도 스레드에서 만들어 녹화 감시를 막지 않습니다 |
| 크기 | 압축 전 64 MiB를 넘으면 두 로그 파일의 줄을 시각 순으로 보아 오래된 줄부터 뺍니다. 뺀 줄 수는 `manifest.json`의 `trimmed`에 적습니다 |
| 기간 | 로그 줄의 `ts`, 다시보기 작업 로그의 `at`이 (지금 − 고른 일수) 이후인 줄만 넣습니다. "전체"는 남아 있는 모든 줄입니다. 진단 요약(`diagnostics/`)은 기간과 상관없이 지금 상태입니다(세션은 최근 200개) |
| 화면 | 기간, "더 가리기" 칸(기본 꺼짐), 내려받기 단추. 단추 옆에 무엇이 들어가고 무엇이 빠지는지와 이 문서 안내를 늘 보입니다. "더 가리기"를 켤 때 "값이 빠져 문제 분석이 어려워진다"는 확인을 한 번 받고, 취소하면 꺼진 채로 둡니다. 끌 때는 묻지 않습니다 |

## 묶음의 파일

| 파일 | 내용 | 원본 |
|---|---|---|
| `manifest.json` | 만든 시각, 프로그램 이름·버전, 고른 기간, 가리기 수준, 파일별 줄 수·크기, 가린 건수, 대신 이름 개수, 읽지 못한 로그 줄 수, 잘라 낸 줄 수, 넣지 않은 것 목록 | 묶음을 만들며 적습니다 |
| `logs/live-recorder.jsonl` | 서버 로그 파일(`live-recorder.log`와 순환된 `live-recorder.log.1`~`.5`)에서 고른 기간의 줄. 한 줄에 JSON 하나, 오래된 것부터 | 로그 폴더. 5 MiB씩 5개 순환 |
| `replay/job-logs.jsonl` | 다시보기·클립·유튜브 영상 작업별 로그에서 고른 기간의 줄. **downloads 확장이 켜져 있을 때만** 들어갑니다(로그 내려받기는 확장이 내놓은 줄을 묶기만 합니다) | `replay/replay.sqlite3`의 `logs` 표. 작업마다 최근 200줄 |
| `diagnostics/summary.json` | 만든 시각, 버전(live_recorder, Python, 운영체제 문자열, ffmpeg·ffprobe의 `-version` 첫 줄), 디스크 사용량(데이터·녹화·로그 폴더의 전체·사용·여유 바이트) | 진단팩과 같은 함수 |
| `diagnostics/settings.json` | 설정값(비밀 아닌 것), 채널별 화질·확장자·자동 녹화·코덱 우선순위, 비밀 저장소 파일이 있는지 여부. 켠 확장의 설정(예: downloads의 `replay_*`)도 같은 자리에 들어갑니다(경로는 설정 여부만) | 진단팩과 같은 함수 |
| `diagnostics/sessions.json` | 최근 세션 200개의 시각·채널·종료 사유·종료코드·길이·크기·수신 방식·마무리 결과·주소 갱신 횟수 | 녹화 저장소의 세션 기록 |
| `diagnostics/log_summary.json` | 고른 기간의 로그에서 경고·오류를 (수준, 로거, 이벤트, 문장 틀, 예외 형식)으로 묶은 횟수와 처음·마지막 시각, 수준별 줄 수 | 진단팩과 같은 함수 |

넣지 않는 것: 쿠키(`secrets/chzzk-cookies.json`), 계정(`secrets/account.json`), 텔레그램 봇 토큰·chat_id(`secrets/telegram.json`), 첫 설정 코드 파일, DB 파일 자체(`recorder.sqlite3`, `replay.sqlite3`), 녹화·다시보기 영상 파일, 진단팩 zip. 묶음의 파일 목록은 위 일곱 개로 정해져 있습니다. downloads 확장이 꺼져 있으면 `replay/job-logs.jsonl`이 없고, `manifest.json`의 넣지 않은 것 목록에서도 `replay.sqlite3`·다시보기 영상이 빠집니다. 진단 요약을 내놓는 확장이 켜져 있으면 `diagnostics/extensions.json`이 더해집니다(들어 있는 세 확장은 내놓지 않습니다).

진단 요약(`diagnostics/`)은 원래 진단팩처럼 채널 이름과 ID를 넣지 않고 채널을 `채널-N`으로 적습니다. 두 수준 모두 같습니다. 로그 줄의 `channel_id`와 이어 보려면 더 가리기를 켭니다(그때 같은 채널은 모든 파일에서 같은 `채널-N`입니다).

## 로그 줄(`logs/live-recorder.jsonl`)의 필드

모든 줄은 `live_recorder/logs.py`의 `JsonFormatter`가 씁니다. 기본 수준에서는 모든 필드가 그대로 남고 늘 가리는 것(아래 "가리기" 절)만 바뀝니다. 표의 "더 가리기" 열은 더 가리기를 켰을 때 그 필드가 어떻게 되는지입니다.

### 모든 줄에 있는 필드

| 필드 | 내용 | 더 가리기 |
|---|---|---|
| `ts` | 기록 시각. UTC ISO 8601, 밀리초까지 | 남는다 |
| `level` | `INFO`·`WARNING`·`ERROR`·`CRITICAL` | 남는다 |
| `logger` | 로거 이름: `live_recorder.engine`(감시·녹화), `live_recorder.receiver`(녹화 수신 프로세스의 표준 오류 출력), `live_recorder.finalize`(마무리), `live_recorder.notify`(텔레그램), `live_recorder.web`(웹·로그인), `live_recorder.store`(설정 저장소), `live_recorder.replay.store`(downloads 확장의 다시보기 저장소 옮기기. 이름은 그대로 둡니다), `live_recorder.events`(사건 알림), `live_recorder.extensions`(확장 등록), `live_recorder.youtube`(유튜브 플랫폼 모듈의 yt-dlp 글), `live_recorder.ext.<이름>`(확장이 남기는 줄) | 남는다 |
| `msg` | 값이 채워진 문장. 파일 이름, 방송 제목, 오류 문장, 도구 출력이 들어 있을 수 있다 | 빠진다 |
| `tmpl` | 값을 채우기 전의 문장 틀(예: `녹화 시작 %s`). 인자가 있는 호출에만 있다 | 남는다. 인자가 없는 호출의 고정 문장(예: `종료 시작`)도 더 가리기에서는 `tmpl`로 남긴다. 코드에 적힌 고정 문장이 아닌 `msg`(수신기 출력, 값을 미리 채운 문장)는 틀 없이 빠진다 |
| `exc` | 예외가 있을 때의 전체 traceback. 서버의 파일 경로와 예외 문장(값이 들어 있을 수 있다)이 들어 있다 | 빠지고 `exc_type`(예외 형식 이름, 예: `ValueError`)만 남는다 |
| `exc_type` | 더 가리기에서만 생긴다. `exc`의 마지막 줄에서 형식 이름만 뗀 것 | (더 가리기에서 생김) |

### 호출이 더하는 필드(`extra`)

| 필드 | 내용 | 더 가리기 |
|---|---|---|
| `event` | 이벤트 이름(아래 표). 프로그램이 정한 영문 이름 | 남는다 |
| `channel_id` | 채널 ID(치지직은 32자리 16진수). 어느 플랫폼의 채널인지는 `platform`에 있다 | `채널-N`. 묶음 안에서 같은 채널은 모든 파일에서 같은 번호다. 녹화 채널을 채널 목록 순서로 먼저 번호를 매기고(진단 요약의 채널 순서와 같다), 다시보기 채널, 그 밖에 나온 ID 순이다 |
| `platform` | 채널에 관한 줄의 플랫폼 식별자(예: `chzzk`). 프로그램이 정한 값이다 | 남는다. 식별 정보가 아니고, 빼면 채널 번호가 어느 플랫폼의 것인지 알 수 없다 |
| `session_id` | 녹화 저장소의 세션 번호(정수) | 남는다 |
| `live_id` | 방송 ID(치지직 방송 번호 liveId, 유튜브 영상 ID) | `방송-N` |
| `client` | 로그인·첫 설정을 한 접속 주소(IPv4·IPv6. 신뢰한 프록시 뒤면 전달된 주소) | `[주소]`(기본 수준에서도 늘 `[주소]`입니다) |
| `fails` | 같은 채널의 연속 실패 횟수 | 남는다 |
| `error_flag` | 연속 실패가 한도에 닿았으면 1, 아니면 0 | 남는다 |
| `source` | 녹화 수신 방식: `live`(일반), `timemachine`(타임머신) | 남는다 |
| `pid` | 녹화 수신 프로세스 번호 | 남는다 |
| `rewind` | 되감기 초(타임머신) | 남는다 |
| `kind` | 녹화가 끝난 방식: `ended`, `user_stop`, `shutdown`, `stalled`, `endlist_again`, `start_failed`, `disconnected` | 남는다 |
| `exit_code` | 녹화 수신 프로세스의 종료코드 | 남는다 |
| `bytes` | 받은 파일 크기(바이트) | 남는다 |
| `end_reason` | 세션 종료 사유: `broadcast_end`, `user_stop`, `stalled`, `error`, `server_shutdown` | 남는다 |
| `result` | 쿠키 확인 결과: `valid`, `expired`, `missing`, `failed` | 남는다 |
| `reason` | 쿠키 확인을 한 까닭: `daily`(하루 한 번), `button`(화면의 확인 단추). 저장소 옮기기 실패(`store_migration_failed`)에서는 SQLite 예외 형식 이름 | 남는다 |
| `notify_event` | 텔레그램 알림 종류: `start_failed`, `abnormal_end`, `timemachine_fallback`, `codec_changed`, `finalize_failed`, `disk_low`, `cookie_expired`, `record_start`, `record_end`, `user_stop`, `finalize_done` 등 | 남는다 |
| `status` | 마무리 결과: `done`, `skipped`, `failed`, `pending`. 접근 키 요청(`api_key_request`)에서는 HTTP 결과 코드 | 남는다 |
| `key_name` | 접근 키에 관리자가 붙인 이름 | 더 가리기의 값 규칙을 따른다 |
| `key_prefix` | 접근 키 값의 앞 8자(`vr_` + 5자. 키로 쓸 수 없는 길이) | 남는다 |
| `scope` | 접근 키의 권한(`view`·`operate`) | 남는다 |
| `method` | 접근 키 요청의 HTTP 방법 | 남는다 |
| `route` | 접근 키 요청의 경로 꼴(`/api/channels/{channel_id}`처럼 값 없이) | 남는다 |
| `from_version` | 저장소를 옮기기 전 형식 번호(정수) | 남는다 |
| `db` | 옮긴 저장소: `replay`(다시보기 저장소). 녹화 저장소의 줄에는 없다 | 남는다 |
| `event_name` | 사건 알림의 사건 이름: `server_started`, `server_stopping`, `channel_added`, `channel_updated`, `channel_removed`, `record_started`, `record_ended`, `finalize_started`, `finalize_ended`, `settings_changed` | 남는다 |
| `extension` | 확장 이름. 프로그램이 정한 영문 이름이다. `extensions_migrated`에서는 켠 확장들을 쉼표로 이은 것 | 남는다 |
| `keys` | `extensions_migrated`에서 확장 설정으로 옮긴 설정 키 수 | 남는다 |
| `channels` | 요청 창구 사건의 감시 중인 채널 수 | 남는다 |
| `interval` | 요청 창구 사건의 채널 사이 간격(초) | 남는다 |
| `cycle` | 요청 창구 사건의 한 바퀴 시간(초) | 남는다 |

이 표에 없는 필드가 줄에 있으면 더 가리기에서는 빼고 `manifest.json`의 `masked.unknown_field`에 센다.

### 이벤트와 문장

`tmpl`의 `%s`·`%d`·`%r` 자리에 들어가는 값은 `msg`에만 있다(더 가리기에서는 빠진다). "값" 열은 기본 수준에서 `msg`에 보이는 것이다.

| 로거 | `event` | 수준 | 문장 틀(`tmpl` 또는 고정 문장) | 값(`msg`에만) | 더하는 필드 |
|---|---|---|---|---|---|
| engine | `started` | INFO | `기동 완료. 채널 %d개` | 채널 수 | — |
| engine | `tool_version` | INFO | `%s: %s` | 도구 이름(ffmpeg·ffprobe·수신 도구), `-version` 첫 줄 | — |
| engine | `stopping` | INFO | `종료 시작` | — | — |
| engine | `stopped` | INFO | `종료 완료` | — | — |
| engine | `record_start` | INFO | `녹화 시작 %s` | 녹화 파일 이름(채널 이름·방송 제목이 들어 있다) | `channel_id`, `platform`, `session_id`, `source`, `pid`, `rewind` |
| engine | `record_end` | INFO 또는 WARNING | `녹화 끝 %s: %s` | 끝난 방식, 상세 문장(종료코드, 도구가 남긴 이유 문장) | `channel_id`, `platform`, `session_id`, `kind`, `exit_code`, `bytes`, `end_reason` |
| engine | `endlist_again` | INFO | `방송 종료로 본다: %s` | 상세 문장 | `channel_id`, `platform`, `live_id` |
| engine | `failure` | WARNING | `실패 %d회: %s` | 실패 횟수, 실패 문장(API 오류, 시작 실패 사유) | `channel_id`, `platform`, `fails`, `error_flag` |
| engine | `disk_low` | ERROR | (값을 채운 문장. 틀 없음) `디스크 여유 부족: N MiB < M MiB` | 여유 공간·한도 | `channel_id`, `platform` |
| engine | `timemachine_fallback` | WARNING | (값을 채운 문장. 틀 없음) `타임머신 실패(…). 일반 수신으로 전환` | API 오류 문장 | `channel_id`, `platform` |
| engine | `codec_changed` | WARNING | `녹화 중 영상 코덱이 바뀌었다 %s → %s` | 앞뒤 코덱 이름(`h264`·`hevc`·`av1`) | `channel_id`, `platform`, `session_id` |
| engine | `codec_change_gap` | WARNING | `코덱이 바뀌어 나눈 파일 사이가 %.1f초 비었다` | 빈 시간(초) | `channel_id`, `platform`, `session_id` |
| engine | `codec_differs` | WARNING | `정한 코덱(%s)과 다른 코덱(%s)을 받는다` | 코덱 이름 | `channel_id`, `platform`, `session_id` |
| engine | `url_refresh` | INFO | `재생 주소 갱신 %s ok=%s` | 갱신 까닭, 성공 여부 | `channel_id`, `platform`, `session_id` |
| engine | `segment_gap` | WARNING | `조각 누락 %s개` | 빠진 조각 수 | `channel_id`, `platform`, `session_id` |
| engine | `worker_exception` | ERROR | `채널 감시 중 예외` | — (`exc`에 traceback) | `channel_id`, `platform` |
| engine | `recover_session` | WARNING | `닫히지 않은 세션을 정리했다` | — | `session_id`, `bytes` |
| engine | `recover_orphan` | WARNING | `기록 없는 .part를 찾았다. 손대지 않았다: %s` | 파일 이름 | `channel_id`, `platform` |
| engine | `refinalize` | INFO | `마무리 실패로 남은 원본을 다시 마무리한다` | — | `session_id` |
| engine | `finalize_exception` | ERROR | `마무리 작업 예외: %r` | 예외 표현(문장에 경로가 있을 수 있다) | — |
| engine | `cookie_check` | INFO | `쿠키 확인 %s (%s)` | 결과, 까닭 | `result`, `reason`, `platform` |
| engine | `cookie_check_exception` | ERROR | `쿠키 확인 중 예외` | — (`exc`) | — |
| engine | `control` | INFO | `조작 %s: %s` | CLI 조작 이름(`start`·`stop`·`remove`·`refinalize:N`), 결과 문장 | `channel_id`, `platform` |
| engine | `gate_paused` | WARNING | `요청 창구를 멈춘다 %g초` | 멈추는 초(5·10·20분, 요청 제한·봇 확인을 받음) | `platform`, `channels`, `interval`, `cycle` |
| engine | `gate_resumed` | INFO | `요청 창구를 다시 연다` | — (찔러 본 확인이 됐다) | `platform`, `channels`, `interval`, `cycle` |
| engine | `gate_cycle_extended` | INFO | `한 바퀴가 스캔 주기보다 길어 주기를 늘렸다: 채널 %d개, 간격 %g초, 한 바퀴 %g초` | 감시 중인 채널 수, 채널 사이 간격, 한 바퀴 시간 | `platform`, `channels`, `interval`, `cycle` |
| engine | `credential_needed` | WARNING | `로그인이 필요한 방송: %s` | `needed`(쿠키 없음) 또는 `rejected`(쿠키 거부) | `channel_id`, `platform`, `reason` |
| engine | `channel_lookup` | INFO | `채널 조회 %s` | 조회로 찾은 채널 ID | `platform`, `channel_id` |
| engine | `channel_lookup_failed` | WARNING | `채널 조회 실패: %s` | 플랫폼 오류 문장 | `platform` |
| engine | `platform_enabled` | INFO | `플랫폼을 켰다` | — (관리자가 고지에 동의하고 자체 점검을 통과함, 켠 시각은 `ts`) | `platform` |
| engine | `platform_disabled` | INFO | `플랫폼을 껐다` | — | `platform` |
| engine | `platform_enable_failed` | WARNING | `플랫폼 켜기 실패: %s` | 자체 점검이 실패한 까닭 | `platform` |
| engine | `control_exception` | ERROR | `조작 처리 중 예외` | — (`exc`) | — |
| youtube | `ytdlp_message` | WARNING 또는 INFO | `yt-dlp: %s` | yt-dlp가 낸 경고·오류 문장(최대 300자). 주소가 들어 있을 수 있다(서명·만료 값은 가린다) | — |
| youtube | `ytdlp_no_js_runtime` | WARNING | `yt-dlp를 자바스크립트 실행기 없이 씁니다(deno를 찾지 못했습니다). 생방송 확인에는 없어도 됩니다` | — (이미지의 deno를 찾지 못했을 때만, 시작할 때 한 번) | — |
| youtube | `ytdlp_selected` | INFO 또는 WARNING | `yt-dlp 고름: %s %s %s` | 서버가 시작할 때 고른 판, 출처(설치된 기본 판·교체한 판), 까닭 문장 | `reason`(`vendor`·`default`) |
| youtube | `ytdlp_fallback` | ERROR | `교체한 yt-dlp를 불러오지 못해 기본 판으로 뜹니다: %s` 또는 `교체한 yt-dlp를 불러오지 못해 기본 판으로 내려갑니다: %s` | 점검 실패 까닭 또는 예외 형식 이름 | — |
| youtube | `ytdlp_update_start` | INFO | `yt-dlp 갱신 시작` | — (관리자가 "yt-dlp 갱신"을 누름) | — |
| youtube | `ytdlp_update_ok` | INFO | `yt-dlp 갱신 성공 %s (yt-dlp-ejs %s)` 또는 `yt-dlp 이미 최신 %s` | 받은 판(또는 최신 판), 보조 묶음 판 | `result`(`ok`·`latest`) |
| youtube | `ytdlp_update_failed` | WARNING | `yt-dlp 갱신 실패: %s` | 실패 까닭(체크섬 불일치, 다른 호스트로 넘김, 크기 초과, 휠 안의 나쁜 경로, 자체 점검 실패 등) | — |
| youtube | `ytdlp_rollback` | INFO | `yt-dlp 기본 판으로 되돌림` | — | — |
| receiver | (없음) | INFO | (틀 없음) | 녹화 수신 프로세스의 표준 오류 한 줄(최대 500자). 도구 메시지, 파일 경로가 들어 있을 수 있다. 주소는 수신 프로세스가 쓰기 전에 스킴과 호스트만 남긴다 | `channel_id`, `platform`, `session_id` |
| finalize | `finalize` | INFO 또는 ERROR | `마무리 %s session=%s %s` | 결과, 세션 번호, 상세 문장(경로·예외 문장이 있을 수 있다) | `session_id`, `channel_id`, `platform`, `status` |
| finalize | `av_length_mismatch` | WARNING | `영상과 음성 길이가 다르다: 영상 %.1f초, 음성 %.1f초` | 합친 파일의 영상·음성 길이(음성을 따로 받은 녹화만) | — |
| finalize | `lowprio_failed` | WARNING | `변환 우선순위를 낮추지 못했다. 그대로 변환한다: %s` | 실행기 경고 문장 | — |
| finalize | (없음) | WARNING | `길이 측정 실패 rc=%s %s` | ffprobe 종료코드, 오류 출력 끝 200자 | — |
| finalize | (없음) | ERROR | `마무리 중 예외 session=%s` | 세션 번호 (`exc`에 traceback) | — |
| notify | `notify_dropped` | WARNING | `알림 대기열이 가득 차 버렸다` | — | `notify_event` |
| notify | `notify_error` | WARNING | `알림 준비 실패: %s` | 예외 형식 이름 | — |
| notify | `notify_failed` | WARNING | `알림 전송 실패: %s` | 실패 상세(HTTP 상태·텔레그램 설명. 토큰이 든 주소는 찍지 않는다) | `notify_event` |
| web | `setup_code_created` | INFO | `첫 설정 코드를 만들었다. 코드는 컨테이너 로그에만 찍었다` | — | — |
| web | `setup_code_disabled` | WARNING | `설정 코드를 끈 상태입니다. 이 주소에 접속할 수 있는 누구나 관리자 계정을 만들 수 있습니다. 계정을 먼저 만드십시오` | — (환경변수 `LIVE_RECORDER_SETUP_CODE=off`로 켠 채 계정 없이 시작할 때마다) | — |
| web | `account_created` | INFO | `환경변수로 관리자 계정을 만들었다` | — | — |
| web | `account_created_setup` | INFO | `첫 설정 화면에서 관리자 계정을 만들었다` | — | `client` |
| web | `setup_locked` | WARNING | `첫 설정 잠금` | — | `client` |
| web | `setup_code_failed` | WARNING | `첫 설정 코드 실패` | — | `client` |
| web | `trusted_proxies_env` | INFO | `환경변수로 신뢰할 프록시를 정했다` | — | — |
| web | `login` | INFO | `로그인` | — | `client` |
| web | `login_locked` | WARNING | `로그인 잠금` | — | `client` |
| web | `login_failed`(로그인), `password_check_failed`(비밀번호 변경의 현재 비밀번호) | WARNING | `비밀번호 확인 실패` | — | `client` |
| web | `password_changed` | INFO | `비밀번호 변경` | — | — |
| web | `log_export` | INFO | `로그 묶음을 내려 줬다` | — | — |
| web | `api_key_created`, `api_key_revoked` | INFO | `접근 키 발급`, `접근 키 폐기` | — | `key_name`, `key_prefix`(발급은 `scope`도) |
| web | `api_key_failed` | WARNING | `접근 키 실패` | — (키 값은 남기지 않는다) | `client` |
| web | `api_key_locked` | WARNING | `접근 키 잠금` | — | `client` |
| web | `api_keys_revoked_all` | INFO | `접근 키 모두 폐기 %s개` | 폐기한 키 수(비밀번호 변경에서 고른 경우) | — |
| web | `secret_file_broken` | ERROR | `비밀 파일을 읽지 못함` | — (파일 내용은 남기지 않는다) | — |
| web | `api_key_request` | INFO | `접근 키 요청 %s %s → %s` | 방법, 경로의 꼴, 결과 코드 | `key_name`, `key_prefix`, `method`, `route`, `status` |
| store | `store_backup` | INFO | `저장소를 옮기기 전 사본을 남겼다` | — (사본은 같은 폴더의 `recorder.sqlite3.schema<옛 형식>.bak`, 권한 0600, 한 번만) | `from_version` |
| store | `store_migrated` | INFO | `저장소를 새 형식으로 옮겼다` | — | `from_version` |
| store | `store_migration_failed` | ERROR | `저장소를 새 형식으로 옮기지 못했다. 원래대로 두었다` | — (서버는 뜨지 않는다) | `from_version`, `reason` |
| replay.store | `store_backup`, `store_migrated`, `store_migration_failed` | INFO·INFO·ERROR | `다시보기 저장소를 옮기기 전 사본을 남겼다`, `다시보기 저장소를 새 형식으로 옮겼다`, `다시보기 저장소를 새 형식으로 옮기지 못했다. 원래대로 두었다` | — (사본은 `replay.sqlite3.schema1.bak`) | `db`, `from_version`(실패하면 `reason`도) |
| events | `event_listener_failed` | WARNING | `사건 처리 중 오류: %s` | 예외 형식 이름 | `event_name`, `extension` |
| events | `event_dropped` | WARNING | `사건 대기열이 가득 차 버렸다` | — | `event_name` |
| extensions | `extensions_migrated` | INFO | `이전 빌드의 설정을 확장 설정으로 옮겼다` | — (이전 빌드에서 올라와 저장소를 한 번 옮길 때. 켠 확장과 옮긴 키 수) | `from_version`, `extension`, `keys` |
| extensions | `extension_skipped` | WARNING | `필요한 확장이 꺼져 있어 켜지 않았다: %s` | 확장 이름 | `extension` |
| extensions | `extension_stop_failed`, `extension_failed` | ERROR | `확장 종료 중 오류`, `확장 잠금 대상 조회 오류`·`확장 채널 정보 오류` 등 | — (`exc`) | `extension` |
| store | `filename_pattern_invalid` | WARNING | `저장된 파일명 패턴을 쓸 수 없어 기본 패턴을 쓴다` | — (저장된 값과 까닭은 화면의 설정에 보입니다) | — |

첫 설정 코드 자체는 로그 파일에 쓰지 않는다. 컨테이너 표준 오류(`docker logs`)에만 한 번 찍힌다. 그 출력은 묶음에 들어가지 않는다.

## 다시보기 작업 로그(`replay/job-logs.jsonl`)의 필드

| 필드 | 내용 | 더 가리기 |
|---|---|---|
| `job` | 작업 ID(무작위 32자리 16진수). 영상 번호·채널과 이어지지 않는다 | 남는다 |
| `at` | 기록 시각(UTC ISO 8601) | 남는다 |
| `level` | `info`·`warning`·`error` | 남는다 |
| `message` | 정해진 문장(예: `작업을 대기열에 등록했습니다.`, `일시정지했습니다(시간대 밖). 받은 부분(1.2 GiB)을 남겼습니다.`, `받아 둔 조각 N개(크기) 다음부터 이어받습니다.`, 유튜브 영상의 `영상 주소를 다시 받아 이어받습니다.`)과 정해진 오류 문장(등). 작업 종류(다시보기·클립·유튜브 영상)는 줄에 적지 않는다. 크기·횟수·시도 번호 같은 숫자가 들어 있다 | 늘 가리는 것과 더 가리기의 바꾸기(아래)를 문장에 건다. 정해진 문장에는 원래 식별 정보가 없다 |


## 가리기

### 늘 가리는 것(두 수준 모두)

모든 파일의 모든 글자에 겁니다.

| 대상 | 찾는 방법 | 바뀐 모양 | `masked`의 이름 |
|---|---|---|---|
| 알고 있는 비밀 값 | 저장한 쿠키 값(쿠키 파일은 줄마다의 값도), 텔레그램 봇 토큰과 chat_id, 첫 설정 코드(대시 있는 꼴과 없는 꼴), 지금 열린 웹 세션 토큰과 CSRF 토큰을 글자 그대로 찾습니다(6자 이상) | `[가림]` | `known_secret` |
| 머리글 값 | `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-CSRF-Token` 뒤의 값(줄 끝이나 따옴표까지) | `[가림]` | `header` |
| 접근 키 값 | `vr_` 뒤에 8자 이상 이어진 값(기록에 남기는 앞 8자 `vr_`+5자는 남긴다) | `vr_[가림]` | `api_key` |
| 쿠키·토큰 이름=값 | `NID_AUT`·`NID_SES` 등 `NID_` 이름, 유튜브 쿠키 이름(`SID`·`HSID`·`SSID`·`APISID`·`SAPISID`와 `__Secure-…` 꼴, `LOGIN_INFO`, `VISITOR_INFO1_LIVE`, `YSC`), `lr_session`, 이름에 SES·SESS·AUT·AUTH·TOKEN·SID·CSRF가 든 것의 `=값`·`: 값`·`"이름": "값"`(값이 8자 이상). `csrf`·`token`·`session`·`lr_session` 뒤에 빈칸을 두고 온 16자 이상의 값 | `[가림]` | `cookie` |
| 텔레그램 봇 토큰 꼴 | 숫자 6~12자리 `:` 영문·숫자 30자 이상(`/bot<토큰>/`처럼 글자 바로 뒤에 붙어 있어도) | `[가림]` | `telegram` |
| 주소 경로의 서명 조각 | 경로에 든 `hdntl=…`, `hdnts=…`, `__gda__=…`, `__bgda__=…`의 값. `~`로 이어진 서명 묶음 전체(`exp=…~acl=/live/*~hmac=…`처럼 `acl`에 `/`가 들어 있어도)를 가립니다 | `[가림]` | `query` |
| 쿠키 파일 줄의 값 | 쿠키 파일(Netscape 꼴) 한 줄의 마지막 칸(값). 저장한 쿠키 파일의 값 하나하나도 알고 있는 비밀로 가린다 | `[가림]` | `cookie` |
| 첫 설정 코드 꼴 | base32 대문자 4자를 대시로 6묶음 이상 이은 것, `설정 코드: 값` | `[가림]` | `setup_code` |
| 주소 질의의 서명·토큰 | 유튜브 재생 주소의 `expire`, `ei`, `ip`, `id`, `sparams`, `lsig`, `pot`, `n`, `mn`, `ms`, `mv`, `pl` 등(플랫폼 모듈의 `query_params`), `__gda__`, `hdnts`, `hdntl`, `token`, `access_token`, `auth`, `sig`, `signature`, `policy`, `key-pair-id`, `key`, `in_key`, `inkey`, `hmac`, `acl`, `exp`, `expires`, `session`, `sid`, `csrf`의 값 | `[가림]` | `query` |
| 유튜브 재생 주소 경로의 값 | `googlevideo.com` 아래 주소의 `/expire/값/`, `/sig/값/`, `/ip/값/`, `/id/값/`, `/sq/값/` 같은 `/이름/값` 꼴(이름은 플랫폼 모듈의 `path_params`). 주소의 호스트와 꼴만 남습니다 | `/이름/[가림]` | `query` |
| 씨미 재생 주소와 재생목록 | `live-video.net` 아래 주소의 경로 값 전부(나눔과 끝의 `.m3u8`·`.mp4` 꼴만 남긴다), 글 속의 `USER-IP="…"` 값(재생목록 본문은 로그에 남기지 않는다). 재생 주소의 `token` 질의 값은 위 질의 규칙으로 가린다 | `[가림]` | `query` |
| 긴 토큰 꼴 | 영문과 숫자가 섞인 40자 이상의 `A-Za-z0-9_-` 덩어리(해시 값도 여기에 걸립니다) | `[가림]` | `token` |
| 접속한 쪽의 주소 | `client` 필드 전체, 글 속의 IPv4(포트 포함)와 IPv6 | `[주소]` | `address` |
| 관리자 사용자 이름 | 계정 파일의 사용자 이름(앞뒤가 글자가 아닌 곳) | `관리자` | `admin` |

치지직 주소, 호스트 이름, 채널 ID·이름, 제목, 파일 이름과 경로, 영상 번호, liveId는 기본 수준에서 그대로 남습니다.

### 더 가리기(선택, 기본 꺼짐)

위에 더해 다음을 바꿉니다.

| 대상 | 바뀐 모양 | `masked`의 이름 |
|---|---|---|
| 로그 줄의 `msg` | 뺍니다. `tmpl`이 있으면 남깁니다. 인자 없는 호출의 고정 문장은 `tmpl`로 남깁니다. 그 밖의 줄(수신기 출력, 값을 미리 채운 `disk_low`·`timemachine_fallback` 문장)은 시각·로거·이벤트·대신 이름만 남습니다 | `message` |
| 로그 줄의 `exc` | `exc_type`(예외 형식 이름)만 남깁니다 | `exception` |
| 채널 ID(`channel_id` 필드와 글 속의 32자리 16진수) | `채널-N` | `channel` |
| 채널 이름(녹화 채널과 다시보기 채널의 저장된 이름, 2자 이상) | 그 채널의 `채널-N` | `channel` |
| 알고 있는 제목(세션 기록과 다시보기 작업에 저장된 제목, 4자 이상) | `[제목]` | `title` |
| `live_id` 필드, 글 속의 `liveId=`·`live_id=` 뒤 숫자, 유튜브의 `v=`·`video_id=`·`live_id=` 뒤 11자 영상 ID(앞이 글자가 아닌 이름만) | `방송-N` | `live` |
| 글 속의 `/video/숫자`, `/clip/…`, `videoNo`·`video_no`·`영상 번호` 뒤 숫자 | `영상-N` | `video` |
| URL(`http://`, `https://`, `ws://`, `wss://`, `ftp://`) | `[주소]` 통째로 | `address` |
| 호스트 이름(점으로 이어진 이름) | `[주소]` | `address` |
| 파일 경로(`/`로 시작하는 경로)와 확장자가 있는 파일 이름 | `[파일].확장자`(확장자가 없으면 `[파일]`) | `file` |
| 위 필드 표에 없는 필드 | 뺍니다 | `unknown_field` |

대신 이름(`채널-N`, `방송-N`, `영상-N`)은 묶음 하나 안에서만 일관됩니다. 다른 때 만든 묶음끼리는 번호가 다를 수 있습니다. `manifest.json`의 `pseudonyms`에 몇 개를 만들었는지 적습니다.

### 더 가리기를 켜도 남는 것

- 시각: 로그 줄의 `ts`, 작업 로그의 `at`, 세션의 시작·끝 시각, 만든 시각. 방송 시각과 녹화 시간대가 드러납니다.
- 이벤트 이름, 문장 틀, 수준, 로거 이름.
- 세션 번호, 작업 ID, 수신 프로세스 번호(`pid`).
- 숫자: 파일 크기(`bytes`), 길이, 종료코드, 실패 횟수, 되감기 초, 조각 수, 주소 갱신 횟수.
- 화질·확장자(설정의 채널별 값), 수신 방식(`live`·`timemachine`), 종료 사유, 마무리 결과, 쿠키 확인 결과.
- 설정값 전체(`diagnostics/settings.json`): 확인 주기, 되감기, 최소 여유 공간, 파일명 패턴의 토큰 목록(글자는 빠짐), 파일 이름과 화면의 날짜·시각 모양(`filename_date_format`·`filename_time_format`·`display_date_format`·`display_time_format`. 목록에서 고른 모양 이름), 저장 경로는 개수·설정 여부만, 알림 켬·끔, 다시보기 시간대 규칙 등. `trusted_proxies` 같은 주소 값은 `[주소]`가 됩니다(이것은 기본 수준에서도 같습니다).
- 비밀 저장소 파일이 있는지 여부(`secrets`: 쿠키·텔레그램·계정·설정 코드 각각 참/거짓).
- 프로그램·Python·ffmpeg·ffprobe 버전, 운영체제 문자열(`platform.platform()`. 커널 버전 등), 디스크 전체·사용·여유 바이트.
- 채널 수와 다시보기 채널 수(대신 이름 개수로).

## `manifest.json`의 필드

| 필드 | 내용 |
|---|---|
| `created_at`, `created_at_seoul` | 만든 시각(UTC, Asia/Seoul) |
| `program` | `name`(VOD Recorder), `version` |
| `range` | `key`(`1`·`3`·`7`·`all`), `days`, `since`(이 시각 이후의 줄) |
| `level`, `level_label` | 고른 가리기 수준: `default`(기본) 또는 `strict`(더 가리기) |
| `files` | 파일마다 `lines`(jsonl 파일의 줄 수), `bytes`(압축 전 크기) |
| `masked` | 위 가리기 표의 이름별 건수 |
| `pseudonyms` | 만든 대신 이름 개수: `channels`, `videos`, `lives`(진단 요약의 채널은 두 수준 모두 대신 이름이라 기본 수준에서도 셉니다) |
| `unreadable_log_lines` | 로그 파일에서 JSON으로 읽지 못해 넣지 않은 줄 수 |
| `trimmed` | `applied`(뺐는지), `limit_bytes`(64 MiB), `dropped_lines`(파일별로 뺀 줄 수) |
| `not_included` | 넣지 않은 것 목록 |

## 진단 요약의 필드

| 파일 | 필드 |
|---|---|
| `diagnostics/summary.json` | `created_at`, `versions`(`live_recorder`, `python`, `platform`, `ffmpeg`, `ffprobe`), `disk`(`data`·`recordings`·`logs`마다 `total`·`used`·`free`) |
| `diagnostics/settings.json` | 설정 키 전부(비밀 아님), `channels`(채널마다 `channel`(채널-N), `quality`, `extension`, `auto_record`, `video_codec`), `secrets`(`cookies`, `telegram`, `account`, `setup_code` 있는지) |
| `diagnostics/sessions.json` | 세션마다 `session`, `channel`(채널-N, 지운 채널은 그래도 번호), `started_at`, `ended_at`, `end_reason`, `exit_code`, `media_seconds`, `bytes`, `source`, `finalize_status`, `clean_exit`, `time_machine_active`, `refreshes` |
| `diagnostics/log_summary.json` | `lines_read`, `levels`(수준별 줄 수), `problems`(경고·오류 묶음마다 `level`, `logger`, `event`, `template`(수신기 줄은 비움), `exception`(형식 이름), `count`, `first`, `last`) |

## 보내기 전에 볼 것

1. `manifest.json`의 `level`이 원하는 수준인지 봅니다. 기본(`default`)이면 채널 이름·제목·파일 이름·서버 경로가 들어 있습니다.
2. 기간이 필요한 만큼인지 봅니다. 길수록 녹화 시간대가 더 많이 드러납니다.
3. `manifest.json`의 `masked`에 `known_secret`·`header`·`cookie`·`api_key`·`telegram`·`setup_code`·`query`가 0이 아니면 로그에 비밀이 섞여 있었다는 뜻입니다. 가려졌지만 원인(어느 줄인지)을 따로 확인합니다.
4. 더 가리기에서 `masked.unknown_field`가 0이 아니면 이 문서에 없는 필드가 있었습니다(그 필드는 뺐습니다). 문서와 `logmask.FIELDS`를 고칩니다.
5. `logs/live-recorder.jsonl`을 열어 보이면 곤란한 이름·제목·경로가 없는지 몇 줄 훑습니다. 특히 `msg`, `exc`, 수신기(`live_recorder.receiver`) 줄, `replay/job-logs.jsonl`의 `message`를 봅니다.
6. 시각·크기·설정값이 남는 것이 괜찮은지 판단합니다(위 "더 가리기를 켜도 남는 것").
