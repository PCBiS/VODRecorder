# VOD Recorder 설치

VOD Recorder는 치지직·유튜브·씨미의 실시간 방송을 녹화하고 다시보기·영상을 받는 도구입니다. 웹 화면으로 씁니다. 컨테이너 이미지로 쓰거나, 리눅스·macOS에 직접 설치해 쓸 수 있습니다.

## 시험한 조합

| 운영체제 | 파이썬 | 비고 |
|---|---|---|
| 리눅스 x86_64 | 3.13, 3.15 | 전체 시험 통과 |
| 리눅스 arm64 | 3.15(컨테이너 이미지) | 이미지는 두 아키텍처를 가리지 않게 만듭니다 |
| macOS(Apple Silicon) | 3.13 이상 | 운영체제에 기대는 부분을 맞춰 두었습니다. 실기 확인 전입니다 |

필요한 파이썬은 **3.13 이상**입니다. 3.12 이하에서는 설치되지 않습니다.

## 1. 컨테이너로 쓰기

```sh
docker build -t vodrecorder .
docker run -d --name vodrecorder -p 127.0.0.1:8080:8080 \
  -v "$PWD/data:/data" -v "$PWD/logs:/logs" -v /srv/recordings:/recordings \
  --stop-timeout 60 vodrecorder
docker logs vodrecorder   # 첫 설정 코드가 찍힙니다
```

이미지 안의 위치는 자료 `/data`, 로그 `/logs`, 녹화 `/recordings`입니다(이미지가 환경변수로 정합니다). 저장소의 `compose.docker.yaml`, `compose.podman.yaml`도 예시로 쓸 수 있습니다.

### ARM(arm64)으로 빌드하기

Dockerfile은 아키텍처를 가리지 않습니다. 기반 이미지는 여러 아키텍처를 담은 것이고, FFmpeg는 소스에서 빌드하며, deno는 x86_64와 aarch64 두 해시를 모두 고정해 둡니다.

```sh
# ARM 장비(예: Apple Silicon Mac, 라즈베리 파이 64비트)에서는 그대로
docker build -t vodrecorder .
# x86_64 장비에서 arm64 이미지를 만들 때(QEMU 에뮬레이션이 필요하고 오래 걸립니다)
docker buildx build --platform linux/arm64 -t vodrecorder:arm64 --load .
```

## 2. 설치해 쓰기 (리눅스·macOS)

### 준비: 파이썬과 FFmpeg

- **리눅스**: 배포판 꾸러미로 설치합니다. 예: `sudo apt install python3 pipx ffmpeg`(Debian·Ubuntu), `sudo dnf install python3 pipx ffmpeg`(Fedora).
  배포판의 파이썬이 3.13보다 낮으면 `uv python install 3.13`이나 배포판의 새 판 꾸러미로 3.13 이상을 준비합니다.
- **macOS**: [Homebrew](https://brew.sh)로 설치합니다. `brew install python@3.13 pipx ffmpeg`, 그다음 `pipx ensurepath`.

FFmpeg는 https와 hls·mp4·mpegts·aac 구성요소가 들어 있어야 합니다. 배포판과 Homebrew의 FFmpeg에는 들어 있습니다. 다른 자리에 둔 FFmpeg를 쓰려면 `LIVE_RECORDER_FFMPEG`, `LIVE_RECORDER_FFPROBE`에 경로를 적습니다.

### pipx로 설치

```sh
pipx install --python python3.13 ./vod-recorder    # 소스 폴더에서(또는 만든 휠 파일 경로)
vod-recorder --version
vod-recorder doctor                                 # 실행 환경 점검
```

`vod-recorder` 명령은 `python -m live_recorder`와 같은 일을 합니다. 유튜브에 쓰는 yt-dlp와 deno도 함께 설치됩니다.

`vod-recorder doctor`는 파이썬 판, ffmpeg·ffprobe와 필요한 구성요소, 유튜브용 도구, 저장 위치의 쓰기 권한과 여유 공간, 포트를 보고 모자란 것을 고치는 방법과 함께 알려 줍니다. 서버를 시작할 때도 ffmpeg·ffprobe나 쓸 수 있는 저장 위치가 없으면 뜨지 않고 까닭을 알립니다. 유튜브용 도구가 없으면 서버는 뜨고 유튜브를 켤 때 알립니다.

### 처음 실행과 첫 설정

```sh
vod-recorder web            # 기본은 127.0.0.1:8080
```

1. 표준 오류(터미널)에 **첫 설정 코드**가 한 번 찍힙니다. 다시 보려면 `vod-recorder account setup-code`.
2. 브라우저로 `http://127.0.0.1:8080`에 들어가 첫 설정 화면에서 코드와 관리자 이름·비밀번호를 넣습니다.
   설정 코드 단계가 필요 없으면 환경변수 `LIVE_RECORDER_SETUP_CODE=off`로 끌 수 있습니다. 끄면 코드 없이 계정을 만들 수 있으므로, 이 주소에 접속할 수 있는 누구나 먼저 계정을 만들 수 있습니다. 서버를 띄운 뒤 곧바로 계정을 만드십시오.
3. 다른 기기에서 쓰려면 역방향 프록시(HTTPS) 뒤에 두고, 직접 열어야 하면 `--host 0.0.0.0`(또는 `LIVE_RECORDER_HOST`)을 씁니다.
4. 이름(도메인)으로 열어 쓴다면 환경변수 `LIVE_RECORDER_ALLOWED_WEB_HOSTS`에 그 이름을 쉼표로 나눠 적어 두기를 권합니다(예: `rec.example.org`). 적어 두면 다른 이름의 `Host`로 온 요청은 400으로 거절합니다. IP 주소와 `localhost`는 늘 받습니다. 비워 두면 모든 이름을 받습니다. 악성 사이트가 자기 이름을 이 서버의 주소로 바꿔 가리키게 하는 공격(DNS 리바인딩)을 막는 데 쓰입니다.

요청 본문은 1 MiB까지 받습니다(쿠키 파일 올리기는 2 MiB). 넘으면 413으로 거절합니다. 동시 연결은 200개까지 받고 넘으면 503입니다. `LIVE_RECORDER_MAX_CONNECTIONS`(20 이상)로 바꿀 수 있습니다.
로그인 실패 제한은 주소마다 셉니다(IPv6는 /64 묶음 단위). 여러 곳에서 틀린 시도가 몰리면 로그인 확인을 조금 늦추고, 확인을 기다리는 요청이 16개를 넘으면 잠시 429로 돌려보냅니다.
녹화·다운로드 하위 프로세스에는 필요한 환경변수만 넘깁니다. 나가는 프록시(`HTTP_PROXY`·`HTTPS_PROXY`·`NO_PROXY`·`ALL_PROXY`)와 사설 인증서(`SSL_CERT_FILE`·`SSL_CERT_DIR`·`REQUESTS_CA_BUNDLE`) 설정은 넘깁니다. 역방향 프록시가 넘기는 `X-Forwarded-For`·`X-Forwarded-Proto`는 설정의 "신뢰할 프록시"(또는 `LIVE_RECORDER_TRUSTED_PROXIES`)에 적은 주소에서 온 요청일 때만 봅니다.

### 자료 위치

정하지 않으면 운영체제 관례를 따릅니다.

| | 리눅스 | macOS |
|---|---|---|
| 자료(설정·기록·비밀 저장소) | `$XDG_DATA_HOME/vod-recorder` (기본 `~/.local/share/vod-recorder`) | `~/Library/Application Support/VOD Recorder` |
| 로그 | `$XDG_STATE_HOME/vod-recorder/logs` (기본 `~/.local/state/vod-recorder/logs`) | `~/Library/Logs/VOD Recorder` |
| 녹화 | `~/Videos/vod-recorder` | `~/Movies/VOD Recorder` |

명령줄(`--data-dir`, `--log-dir`, `--record-root`)이나 환경변수(`LIVE_RECORDER_DATA_DIR`, `LIVE_RECORDER_LOG_DIR`, `LIVE_RECORDER_RECORD_ROOT`)로 바꿀 수 있습니다. 자료 폴더만 정하면 로그는 그 아래 `logs`에 둡니다. 만든 폴더와 파일은 실행한 사용자만 읽을 수 있습니다.

### 서비스로 등록

**리눅스(systemd 사용자 서비스)**: 저장소의 `packaging/systemd/vod-recorder.service`를 씁니다.

```sh
mkdir -p ~/.local/share/vod-recorder ~/.local/state/vod-recorder ~/Videos/vod-recorder ~/.config/systemd/user
cp packaging/systemd/vod-recorder.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vod-recorder
loginctl enable-linger "$USER"          # 로그아웃한 뒤에도 돌게
journalctl --user -u vod-recorder       # 첫 설정 코드와 서버 메시지
```

이 단위 파일은 새 권한을 얻지 못하게 하고(`NoNewPrivileges`), 시스템을 읽기 전용으로 두며(`ProtectSystem=strict`), 쓸 수 있는 경로를 위의 세 폴더로 한정하고(`ReadWritePaths`), 전용 임시 폴더(`PrivateTmp`)를 씁니다. 위치를 바꾸면 `Environment`와 `ReadWritePaths`를 함께 고칩니다. 쓸 경로는 미리 만들어 두어야 합니다.

**macOS(launchd)**: 저장소의 `packaging/launchd/dev.vodrecorder.plist`에서 `USERNAME`을 바꾼 뒤 씁니다.

```sh
mkdir -p ~/Library/Logs/"VOD Recorder"
cp packaging/launchd/dev.vodrecorder.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.vodrecorder.plist
cat ~/Library/Logs/"VOD Recorder"/launchd.err.log   # 첫 설정 코드
launchctl bootout gui/$(id -u)/dev.vodrecorder      # 멈추기
```

### 갱신과 제거

```sh
pipx upgrade vod-recorder            # 또는 새 판의 소스·휠로 pipx install --force
pipx uninstall vod-recorder          # 프로그램만 지웁니다. 자료·로그·녹화 폴더는 남습니다
```

서비스로 등록했다면 갱신한 뒤 `systemctl --user restart vod-recorder`(macOS는 bootout 뒤 bootstrap)로 다시 시작합니다. 유튜브용 yt-dlp는 설정 화면에서 더 새 판으로 바꿔 쓸 수 있고, 되돌리면 설치된 기본 판을 씁니다.

## 3. 윈도우

윈도우는 직접 지원하지 않습니다. 실행하면 안내를 보이고 끝납니다. WSL(Windows Subsystem for Linux)에 리눅스를 설치해 그 안에서 위의 리눅스 방법을 따르거나, Docker Desktop으로 컨테이너 이미지를 쓰십시오.

## 라이선스

VOD Recorder는 [Unlicense](../LICENSE)입니다. 컨테이너 이미지에는 FFmpeg(LGPL 2.1 이상) 등 다른 프로젝트의 소프트웨어가 들어 있으며, 그 고지는 [THIRD-PARTY.md](THIRD-PARTY.md)와 이미지 안의 `/usr/share/licenses/`에 있습니다. 이미지를 만들 때 `--build-arg SOURCE_URL=<저장소 주소>`를 주면 이미지 라벨에 소스 주소와 고지 문서의 주소가 들어갑니다.
