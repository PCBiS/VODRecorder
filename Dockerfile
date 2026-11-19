# syntax=docker/dockerfile:1.7
# VOD Recorder(코드 이름 live-recorder) 컨테이너 이미지.
#
# 버전 고정:
#   - Python 3.15.2 slim(Debian trixie). 태그와 다중 아키텍처 인덱스 다이제스트를 함께 적는다(2026-11-11 Docker Hub 확인).
#   - FFmpeg 9.0.2를 공식 원본에서 받아 SHA-256을 확인한 뒤 빌드한다.
#   - Python 의존성은 requirements.txt의 고정 버전. 유튜브 도구(yt-dlp, yt-dlp-ejs, deno)는 판과 SHA-256을 함께 고정한다.
#     /opt/venv는 root 소유라 실행 사용자(10001)가 쓸 수 없다. 풀린 크기 약 107MB(deno 96MB)
ARG PYTHON_IMAGE=python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
ARG FFMPEG_VERSION=9.0.2
ARG FFMPEG_SHA256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e

# ---- FFmpeg ----
FROM ${PYTHON_IMAGE} AS ffmpeg
ARG FFMPEG_VERSION
ARG FFMPEG_SHA256
RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential nasm pkg-config libssl-dev zlib1g-dev xz-utils curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSLo ffmpeg.tar.xz "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
 && echo "${FFMPEG_SHA256}  ffmpeg.tar.xz" | sha256sum -c - \
 && tar xJf ffmpeg.tar.xz --strip-components=1 \
 && rm ffmpeg.tar.xz
# 외부 코덱 라이브러리 없이 FFmpeg 자체 구성요소만 쓴다. https는 OpenSSL로 한다(9.0.2 configure에서 OpenSSL은 nonfree·version3 목록에 없다. 결과물은 LGPL 2.1 이상).
# 자동 탐지를 끄므로 압축된 moov와 gzip 응답에 쓰는 zlib은 직접 켠다. 실행 이미지에는 libssl·zlib이 기본으로 있다.
# 이 도구가 쓰는 것: hls·mov·mpegts·concat 디먹서, mp4·mpegts 먹서, https·tls·tcp·crypto·pipe·file 프로토콜, ffprobe
RUN ./configure --prefix=/opt/ffmpeg --disable-autodetect --disable-doc --disable-debug --disable-ffplay \
      --enable-openssl --enable-zlib --enable-pthreads \
 && make -j"$(nproc)" \
 && make install \
 && strip /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe \
 && /opt/ffmpeg/bin/ffmpeg -hide_banner -version | head -1

# ---- Python 의존성 ----
FROM ${PYTHON_IMAGE} AS deps
# 유튜브 도구 세 가지는 PyPI의 공식 묶음을 해시로 고정해 먼저 깐다(의존 없이, 해시가 다르면 빌드가 멈춘다).
# 판은 requirements.txt와 같아야 한다(tests/test_youtube_enable.py가 견준다). deno는 아키텍처마다 휠이 달라 두 해시를 함께 적는다
# (PyPI 묶음 정보 2026-11-13: manylinux_2_27_x86_64, manylinux_2_27_aarch64). 그 뒤 requirements.txt는 이미 깔린 판을 그대로 쓴다
ARG YTDLP_VERSION=2026.10.26
ARG YTDLP_SHA256=a05c5b5d8562f7e8a62f5334348b406af4930d56d3a65f8021d14af0e22339ae
ARG YTDLP_EJS_VERSION=0.8.0
ARG YTDLP_EJS_SHA256=79300e5fca7f937a1eeede11f0456862c1b41107ce1d726871e0207424f4bdb4
ARG DENO_VERSION=2.9.8
ARG DENO_SHA256_X86_64=b1d1318e7deda8efd593aefa8b565a827fe71cf83aeff09d3a5ceb047444ca18
ARG DENO_SHA256_AARCH64=3e286918a36f436134336c5c6ef37dfe3a27f16ea22cd6af452ab7daa4ef1da4
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
 && printf '%s\n' \
      "yt-dlp==${YTDLP_VERSION} --hash=sha256:${YTDLP_SHA256}" \
      "yt-dlp-ejs==${YTDLP_EJS_VERSION} --hash=sha256:${YTDLP_EJS_SHA256}" \
      "deno==${DENO_VERSION} --hash=sha256:${DENO_SHA256_X86_64} --hash=sha256:${DENO_SHA256_AARCH64}" \
      > /tmp/youtube-tools.txt \
 && /opt/venv/bin/pip install --no-cache-dir --no-compile --no-deps --only-binary=:all: --require-hashes -r /tmp/youtube-tools.txt \
 && /opt/venv/bin/pip install --no-cache-dir --no-compile -r /tmp/requirements.txt \
 && /opt/venv/bin/deno --version

# ---- 실행 이미지 ----
FROM ${PYTHON_IMAGE}
ARG FFMPEG_VERSION
# 저장소 주소(예: https://github.com/<계정>/<저장소>). 주면 source·url·documentation 라벨에 들어가고, 없으면 빈 라벨이다
ARG SOURCE_URL=""
LABEL org.opencontainers.image.title="VOD Recorder" \
      org.opencontainers.image.description="치지직·유튜브·씨미 실시간 녹화와 다시보기·영상 받기 도구" \
      io.github.vodrecorder.ffmpeg.version="${FFMPEG_VERSION}" \
      org.opencontainers.image.licenses="Unlicense AND LGPL-2.1-or-later" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.url="${SOURCE_URL}" \
      org.opencontainers.image.documentation="${SOURCE_URL:+${SOURCE_URL}/blob/main/docs/THIRD-PARTY.md}"
# 기반 이미지가 만들어진 뒤에 나온 Debian 보안 갱신을 받는다(기반 이미지는 다이제스트로 고정돼 있어 스스로 새로워지지 않는다).
# FFmpeg는 libssl·zlib을 공유 라이브러리로 쓰므로 여기서 받은 판을 그대로 쓴다
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 recorder \
 && useradd --system --uid 10001 --gid recorder --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin recorder \
 && mkdir -p /data /recordings /logs \
 && chown recorder:recorder /data /recordings /logs
COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe /usr/local/bin/
# 라이선스 전문: FFmpeg(LGPL 2.1 이상)가 소스와 함께 배포하는 두 파일, 이 프로그램의 LICENSE(Unlicense)와 다른 소프트웨어 고지
COPY --from=ffmpeg /src/COPYING.LGPLv2.1 /src/LICENSE.md /usr/share/licenses/ffmpeg/
COPY LICENSE docs/THIRD-PARTY.md /usr/share/licenses/vodrecorder/
COPY --from=deps /opt/venv /opt/venv
COPY live_recorder /app/live_recorder
# 소스 파일의 원래 권한(예: 0600)이 따라와도 실행 사용자 10001이 읽을 수 있게 한다(0600 파일이면 기동하지 못한다).
# 쓰기는 root만 남긴다. COPY --chmod는 폴더에도 같은 값을 주므로 쓰지 않는다
RUN chmod -R u=rwX,go=rX /app \
 && chmod -R a=rX /usr/share/licenses/ffmpeg /usr/share/licenses/vodrecorder
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    LIVE_RECORDER_DATA_DIR=/data \
    LIVE_RECORDER_RECORD_ROOT=/recordings \
    LIVE_RECORDER_LOG_DIR=/logs \
    LIVE_RECORDER_HOST=0.0.0.0 \
    LIVE_RECORDER_PORT=8080
WORKDIR /app
USER 10001:10001
VOLUME ["/data", "/recordings", "/logs"]
EXPOSE 8080
# SIGTERM을 받으면 녹화 프로세스를 정상 종료시키고 파일을 닫는다. 실행할 때 유예 시간을 60초로 준다(README)
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python", "-m", "live_recorder.healthcheck"]
ENTRYPOINT ["python", "-m", "live_recorder"]
CMD ["web", "--quiet"]
