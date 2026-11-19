# 컨테이너 이미지에 들어 있는 다른 프로젝트의 소프트웨어

이 저장소가 `ghcr.io`에 올리는 VOD Recorder 컨테이너 이미지에는 VOD Recorder 말고도 아래 소프트웨어가 들어 있습니다. VOD Recorder 자체는 [Unlicense](../LICENSE)이지만, 아래 소프트웨어는 각자의 라이선스를 따릅니다. 이미지를 다시 배포할 때는 이 문서를 함께 두십시오.


## FFmpeg (LGPL 2.1 이상)

이미지의 `/usr/local/bin/ffmpeg`와 `/usr/local/bin/ffprobe`는 [FFmpeg](https://ffmpeg.org) 프로젝트의 소프트웨어입니다. FFmpeg는 [GNU Lesser General Public License 2.1](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html) 또는 그 이후 판으로 배포됩니다. FFmpeg의 저작권은 FFmpeg 개발자들에게 있습니다.

| 항목 | 값 |
|---|---|
| 판 | 9.0.2 |
| 소스 | <https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz> |
| 소스 SHA-256 | `8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e` |
| 고친 곳 | 없음. 공식 소스를 그대로 빌드합니다 |
| 빌드 옵션 | `--disable-autodetect --disable-doc --disable-debug --disable-ffplay --enable-openssl --enable-zlib --enable-pthreads` |

- `--enable-gpl`, `--enable-version3`, `--enable-nonfree`는 쓰지 않습니다. 외부 코덱 라이브러리도 넣지 않습니다. 따라서 결과물은 LGPL 2.1 이상입니다.
- VOD Recorder는 FFmpeg를 별도 프로그램으로 실행할 뿐 FFmpeg의 코드를 자기 안에 넣지 않습니다.
- **소스를 받는 방법**: 이미지를 올린 판마다 그 이미지에 쓴 FFmpeg 소스 묶음(`ffmpeg-9.0.2.tar.xz`)을 이 저장소의 릴리스에 함께 올립니다. 위 공식 주소에서도 받을 수 있고, 이 저장소의 `Dockerfile`로 같은 판을 같은 옵션으로 직접 빌드할 수 있습니다.
- **라이선스 전문**: 이미지 안의 `/usr/share/licenses/ffmpeg/`에 FFmpeg가 배포하는 `COPYING.LGPLv2.1`과 `LICENSE.md`가 들어 있습니다. VOD Recorder의 라이선스와 이 문서는 `/usr/share/licenses/vodrecorder/`에 있습니다.
- **바꿔 쓰기**: 다른 FFmpeg를 쓰려면 이미지 안의 두 파일을 바꾸거나, 환경변수 `LIVE_RECORDER_FFMPEG`·`LIVE_RECORDER_FFPROBE`에 다른 경로를 적습니다.

## 기반 이미지 (Debian, Python)

이미지는 Docker 공식 이미지 `python:3.15.2-slim-trixie`를 바탕으로 합니다. 여기에는 Debian 13의 기본 패키지(GNU C 라이브러리, coreutils, bash, OpenSSL, zlib 등)와 CPython이 들어 있습니다. 이미지를 만들 때 `apt-get upgrade`로 그 시점까지 나온 Debian 보안 갱신을 받습니다.

- 패키지마다 라이선스가 다릅니다(GPL, LGPL, BSD, Apache-2.0 등). 패키지별 저작권과 라이선스 고지는 이미지 안의 `/usr/share/doc/<패키지>/copyright`에 있습니다.
- 소스는 Debian이 제공합니다: <https://sources.debian.org>. 이미지에 든 패키지 목록과 판은 `docker run --rm --entrypoint dpkg <이미지> -l`로 볼 수 있습니다.
- CPython은 [PSF 라이선스](https://docs.python.org/3/license.html)입니다.
- 이 저장소는 이 패키지들을 고치지 않습니다.

## 파이썬 라이브러리

`/opt/venv`에 PyPI에서 받은 그대로 들어 있습니다. 각 라이브러리의 라이선스 파일은 이미지 안의 `/opt/venv/lib/python3.15/site-packages/<이름>-<판>.dist-info/`에 있습니다.

| 라이브러리 | 라이선스 |
|---|---|
| FastAPI, Pydantic, pydantic-core, anyio, h11, annotated-types, typing-inspection | MIT |
| Starlette, Uvicorn, httpx, httpcore, click, idna | BSD-3-Clause |
| typing_extensions | PSF-2.0 |
| certifi | MPL-2.0 |
| yt-dlp | Unlicense |
| yt-dlp-ejs | Unlicense, MIT, ISC |
| deno (PyPI 꾸러미에 든 Deno 실행 파일) | MIT. Deno가 포함하는 구성요소(V8 등)는 각자의 라이선스 |

정확한 목록과 판은 `docker run --rm --entrypoint /opt/venv/bin/pip <이미지> list`로 볼 수 있습니다.

## 보증

위 소프트웨어는 모두 각 라이선스가 정한 대로 보증 없이 제공됩니다.
