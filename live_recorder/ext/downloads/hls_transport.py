"""네이버 HLS 서명 전송. 서명된 재생목록은 메모리에만 두고 ffmpeg 표준 입력으로 넘긴다.

- `requests` 대신 `httpx`를 쓴다(이 도구의 다른 HTTP 요청과 같은 의존성).
- `_lsu_sa_` 서명이 없는 HLS(빠른 다시보기, 경로에 토큰이 든 주소)는 `inherit_query=False`로 받는다.
  이때는 조각 주소를 절대 주소로만 바꾸고 질의 문자열을 물려주지 않는다.
- ffmpeg `-progress`로 처리한 시간을 알려 준다.
- 시험용 로컬 HTTP 서버를 허용하는 스위치(`ALLOW_LOCAL_HTTP`)를 두었다. 평소에는 꺼져 있다.
"""
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit

import httpx

from ...core import httpread

from .errors import ReplayFailure, classify_exception, record_diagnostic

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
ALLOW_LOCAL_HTTP = False  # 시험 전용. 작업(job)에 allow_http가 있을 때만 수신 프로세스 안에서 켠다
MAX_REWRITTEN_BYTES = 32 * 1024 * 1024


def _origin(url):
    try:
        parsed = urlsplit(url)
        if ALLOW_LOCAL_HTTP and parsed.scheme == 'http' and parsed.hostname == '127.0.0.1':
            return parsed.hostname, parsed.port
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.port not in (None, 443):
            raise ValueError()
        return parsed.hostname.lower(), 443
    except ValueError:
        raise ReplayFailure('protocol_denied') from None


def _query_key(component):
    return unquote_plus(component.split('=', 1)[0])


def _naver_origin(url):
    origin = _origin(url)
    host = origin[0]
    if ALLOW_LOCAL_HTTP and host == '127.0.0.1':
        return origin
    if not (host == 'pstatic.net' or host.endswith('.pstatic.net')):
        raise ReplayFailure('protocol_denied')
    return origin


def is_signed_naver_hls(url):
    try:
        _naver_origin(url)
        return urlsplit(url).path.lower().endswith('.m3u8') and any(
            _query_key(part) == '_lsu_sa_' for part in urlsplit(url).query.split('&'))
    except (ReplayFailure, TypeError):
        return False


def _media_url(parent, child, inherit_query=True):
    absolute = urljoin(parent, child)
    child_origin = _naver_origin(absolute)
    if child_origin != _naver_origin(parent) or not inherit_query:
        return absolute
    parsed = urlsplit(absolute)
    query = parsed.query.split('&') if parsed.query else []
    keys = {_query_key(part) for part in query}
    for part in urlsplit(parent).query.split('&'):
        key = _query_key(part)
        if part and key not in keys:
            query.append(part)
            keys.add(key)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '&'.join(query), ''))


# KEY·MAP 줄은 속성 단위로 읽고 정해진 속성만 다시 적는다(같은 속성이 둘이거나 모르는 꼴이면 받지 않는다)
_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"\r\n]*"|[^",\s]*)')
TAG_ATTRS = {'#EXT-X-KEY': ('METHOD', 'URI', 'IV', 'KEYFORMAT', 'KEYFORMATVERSIONS'), '#EXT-X-MAP': ('URI', 'BYTERANGE')}


def parse_attrs(text):
    """`A=1,B="x"` → [(이름, 값)]. 빈틈없이 이어지지 않거나 같은 이름이 둘이면 ReplayFailure."""
    out, pos = [], 0
    while pos < len(text):
        m = _ATTR.match(text, pos)
        if not m:
            raise ReplayFailure('invalid_media')
        out.append((m.group(1), m.group(2)))
        pos = m.end()
        if pos < len(text):
            if text[pos] != ',':
                raise ReplayFailure('invalid_media')
            pos += 1
    names = [n for n, _ in out]
    if len(names) != len(set(names)):
        raise ReplayFailure('invalid_media')
    return out


def safe_tag(line, rewrite_uri):
    """KEY·MAP 줄을 새로 조립한다. URI는 정확히 하나(KEY의 METHOD=NONE이면 없음)이고 rewrite_uri로 바꾼 값을 쓴다.
    모르는 속성은 버린다."""
    tag, sep, rest = line.partition(':')
    allowed = TAG_ATTRS.get(tag)
    if allowed is None or not sep:
        raise ReplayFailure('invalid_media')
    attrs = dict(parse_attrs(rest))
    uri = attrs.get('URI')
    if tag == '#EXT-X-KEY' and attrs.get('METHOD') == 'NONE':
        if uri is not None:
            raise ReplayFailure('invalid_media')
        return '#EXT-X-KEY:METHOD=NONE'
    if uri is None or len(uri) < 2 or not (uri.startswith('"') and uri.endswith('"')):
        raise ReplayFailure('invalid_media')
    attrs['URI'] = '"' + rewrite_uri(uri[1:-1]) + '"'
    if '"' in attrs['URI'][1:-1]:
        raise ReplayFailure('invalid_media')
    return tag + ':' + ','.join(f'{name}={attrs[name]}' for name in allowed if name in attrs)


def rewrite_media_playlist(content, manifest_url, inherit_query=True):
    """조각 주소와 KEY/MAP 주소를 절대 주소로 바꾼다. 서명된 질의는 그대로 물려준다."""
    if inherit_query and not is_signed_naver_hls(manifest_url):
        raise ReplayFailure('protocol_denied')
    _naver_origin(manifest_url)
    if isinstance(content, bytes):
        if len(content) > MAX_MANIFEST_BYTES:
            raise ReplayFailure('invalid_media')
        try:
            content = content.decode('utf-8-sig')
        except UnicodeError:
            raise ReplayFailure('invalid_media') from None
    elif not isinstance(content, str) or len(content.encode('utf-8')) > MAX_MANIFEST_BYTES:
        raise ReplayFailure('invalid_media')
    if httpread.has_control(content):
        raise ReplayFailure('invalid_media')  # 제어문자가 든 재생목록은 통째로 받지 않는다
    lines = content.splitlines()
    if not lines or lines[0].strip() != '#EXTM3U' or '#EXT-X-ENDLIST' not in {line.strip() for line in lines}:
        raise ReplayFailure('invalid_media')
    if any(line.strip().startswith(('#EXT-X-STREAM-INF:', '#EXT-X-I-FRAME-STREAM-INF:', '#EXT-X-MEDIA:', '#EXT-X-SESSION-KEY:', '#EXT-X-SESSION-DATA:')) for line in lines):
        raise ReplayFailure('invalid_media')
    result, media_count, output_size = [], 0, 0
    for raw in lines:
        line = raw.strip()
        if line and not line.startswith('#'):
            line = _media_url(manifest_url, line, inherit_query)
            media_count += 1
        elif line.startswith(('#EXT-X-KEY:', '#EXT-X-MAP:')):
            line = safe_tag(line, lambda uri: _media_url(manifest_url, uri, inherit_query))
        output_size += len(line.encode('utf-8')) + 1
        if output_size > MAX_REWRITTEN_BYTES:
            raise ReplayFailure('invalid_media')
        result.append(line)
    if not media_count or not any(line.startswith('#EXTINF:') for line in lines):
        raise ReplayFailure('invalid_media')
    rewritten = ('\n'.join(result) + '\n').encode('utf-8')
    if len(rewritten) > MAX_REWRITTEN_BYTES:
        raise ReplayFailure('invalid_media')
    return rewritten


def _section_args(section):
    if not section:
        return [], []
    match = re.fullmatch(r'(\d{2}):([0-5]\d):([0-5]\d)[~-](\d{2}):([0-5]\d):([0-5]\d)', section.strip())
    if not match:
        raise ReplayFailure('invalid_media')
    a, b, c, d, e, f = map(int, match.groups())
    duration = d * 3600 + e * 60 + f - (a * 3600 + b * 60 + c)
    if duration <= 0:
        raise ReplayFailure('invalid_media')
    return ['-ss', f'{a:02}:{b:02}:{c:02}'], ['-t', str(duration)]


def _fetch_manifest(url, headers):
    # 리다이렉트 대상에 헤더를 넘기지 않는다.
    with httpx.stream('GET', url, headers=dict(headers or {}, **httpread.TEXT_ENCODINGS), timeout=15,
                      follow_redirects=False) as response:
        if 300 <= response.status_code < 400:
            raise ReplayFailure('protocol_denied')
        response.raise_for_status()
        try:
            return b''.join(httpread.iter_body(response, MAX_MANIFEST_BYTES))  # 풀어 가며 센다
        except httpread.TooLarge:
            raise ReplayFailure('invalid_media') from None


def _has_extensionless_media(payload):
    # Called only after every media/key/map URI has passed rewrite validation.
    # Stream the bounded RAM payload; avoid another full manifest text/list.
    for line in BytesIO(payload):
        line = line.strip()
        if line and not line.startswith(b'#'):
            basename = urlsplit(line.decode('utf-8')).path.rsplit('/', 1)[-1]
            if basename and '.' not in basename:
                return True
    return False


@lru_cache(maxsize=16)
def _supports_extension_picky(ffmpeg):
    # Offline capability detection supports both older FFmpeg 5 builds and the
    # hardened FFmpeg 9 image without putting a signed URL in a subprocess argv.
    try:
        result = subprocess.run([str(ffmpeg), '-hide_banner', '-h', 'demuxer=hls'],
                                capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(
        re.search(rb'^\s*-extension_picky\s', result.stdout, re.MULTILINE))


def run_signed_hls(input_url, output_file, section, ffmpeg, headers, subprocess_module, set_processes, clear_processes,
                   inherit_query=True, on_progress=None, output_format=None):
    if inherit_query and not is_signed_naver_hls(input_url):
        raise ReplayFailure('protocol_denied')
    before, after = _section_args(section)
    try:
        payload = rewrite_media_playlist(_fetch_manifest(input_url, headers), input_url, inherit_query)
    except ReplayFailure:
        raise
    except Exception as exc:
        raise ReplayFailure(classify_exception(exc)) from None
    input_options = []
    if _has_extensionless_media(payload):
        input_options = ['-format_whitelist', 'hls,mpegts,mov,aac']
        if _supports_extension_picky(str(ffmpeg)):
            input_options += ['-extension_picky', '0']
    # Headers are deliberately absent: this input is a pipe, and each remote
    # segment authenticates with its own inherited signed query.
    protocols = 'https,tls,tcp,crypto,pipe' + (',http' if ALLOW_LOCAL_HTTP else '')
    progress = ['-progress', 'pipe:1', '-nostats'] if on_progress else []
    fmt = ['-f', output_format] if output_format else []
    if output_format == 'mp4':
        fmt = ['-movflags', '+faststart', *fmt]
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', *progress, *before,
               '-protocol_whitelist', protocols, *input_options, '-f', 'hls', '-i', 'pipe:0',
               *after, '-c', 'copy', *fmt, '-y', str(output_file)]
    process = subprocess_module.Popen(command, stdin=subprocess_module.PIPE, stderr=subprocess_module.PIPE,
                                      stdout=subprocess_module.PIPE if on_progress else None)
    diagnostics = {}

    def write_manifest():
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    def drain_stderr():
        tail = b''
        while True:
            chunk = process.stderr.read(4096)
            if not chunk:
                break
            record_diagnostic(diagnostics, tail + chunk)
            tail = chunk[-256:]

    def read_progress():
        for raw in process.stdout:
            line = raw.decode('ascii', 'replace').strip()
            if line.startswith('out_time_us=') and line[12:].lstrip('-').isdigit():
                on_progress(max(0, int(line[12:])) / 1e6)

    writer = threading.Thread(target=write_manifest, daemon=True)
    reader = threading.Thread(target=drain_stderr, daemon=True)
    progress_reader = threading.Thread(target=read_progress, daemon=True) if on_progress else None
    try:
        set_processes([process])
        reader.start()
        writer.start()
        if progress_reader:
            progress_reader.start()
        returncode = process.wait()
    finally:
        # The worker process group owns cancellation. On local Python errors,
        # close the native consumer so neither private pipe thread can linger.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess_module.TimeoutExpired:
                process.kill()
                process.wait()
        if writer.ident is not None:
            writer.join(timeout=5)
        if reader.ident is not None:
            reader.join(timeout=5)
        if not reader.is_alive():
            process.stderr.close()
        clear_processes()
    if returncode:
        raise ReplayFailure(diagnostics.get('code', 'engine_operation_failed'))
    if not Path(output_file).is_file() or Path(output_file).stat().st_size < 1024:
        raise ReplayFailure('invalid_media')
    return True
