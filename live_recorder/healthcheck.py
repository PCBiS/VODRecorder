"""컨테이너 헬스체크. `GET /status`가 200이면 0으로 끝난다. 이미지의 HEALTHCHECK가 부른다."""

import os
import sys
import urllib.request


def main() -> int:
    port = int(os.environ.get("LIVE_RECORDER_PORT", "8080"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as resp:  # noqa: S310 - 고정된 로컬 주소
            return 0 if resp.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
