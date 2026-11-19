"""정해진 공개 오류 코드. 도구의 진단 문장은 프로세스 메모리 밖으로 나가지 않는다.

로그인이 필요한 영상용 `login_required`, 잘못된 주소용 `invalid_url`, 구간 형식용 `invalid_section`이 있다.
"""

import errno
import re
import ssl
import subprocess

ERROR_MESSAGES = {
    'http_400': '영상 서버가 요청을 거부했습니다. 영상을 다시 분석한 뒤 재시도해 주세요.',
    'http_401': '영상 서버 인증에 실패했습니다. 로그인·쿠키 상태를 확인해 주세요.',
    'http_403': '영상 서버에서 접근을 거부했습니다. 영상 공개 범위와 인증 상태를 확인해 주세요.',
    'login_required': '로그인이나 성인 인증이 필요한 영상입니다. 쿠키가 없거나 만료됐는지 확인해 주세요.',
    'not_found': '영상을 찾지 못했습니다. 삭제됐거나 비공개일 수 있습니다.',
    'not_ready': '아직 다시보기를 받을 수 없는 영상입니다. 잠시 뒤 다시 분석해 주세요.',
    'invalid_url': '지원하는 치지직 다시보기나 클립 주소가 아닙니다.',
    'invalid_section': '구간은 HH:MM:SS~HH:MM:SS 형식이고 끝이 시작보다 늦어야 합니다.',
    'network_timeout': '영상 서버 연결 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.',
    'tls_error': '영상 서버의 보안 연결을 확인하지 못했습니다.',
    'protocol_denied': '지원하지 않는 전송 주소 또는 프로토콜입니다.',
    'tool_missing': '다운로드에 필요한 실행 도구를 찾지 못했습니다.',
    'storage_unavailable': '저장 공간 또는 저장 경로에 문제가 있습니다.',
    'insufficient_space': '다운로드를 시작하기에 저장 공간이 부족합니다.',
    'invalid_media': '다운로드한 파일에서 유효한 영상을 확인하지 못했습니다.',
    'quality_changed': '선택한 품질이 변경되었습니다. 영상을 다시 분석해 주세요.',
    'engine_operation_failed': '영상 처리에 실패했습니다. 다시 시도해 주세요.',
    'extension_disabled': '확장이 꺼져 있어 진행할 수 없습니다',
    # 음성이 따로인 받기, 주소 만료, 요청 제한, 방송 중인 영상, 받지 않는 주소(작업 종류가 문장을 바꿀 수 있다)
    'http_410': '영상 서버의 주소가 더는 유효하지 않습니다. 다시 시도해 주십시오.',
    'url_expired': '영상 주소의 유효 시간이 지났습니다. 다시 시도해 주십시오.',
    'merge_failed': '받은 영상과 음성을 합치지 못했습니다. 받은 파일은 남겨 두었으니 다시 시도하면 합치기부터 합니다.',
    'rate_limited': '영상 서버가 요청을 제한했습니다. 잠시 뒤 다시 시도해 주십시오.',
    'live_video': '방송 중이거나 예정된 영상은 받지 않습니다.',
    'unsupported_url': '이 주소는 받지 않습니다. 영상 하나의 주소를 넣어 주십시오.',
    'login_rejected': '로그인이 필요한 영상인데 쿠키가 거부됐습니다. 쿠키가 만료됐거나 권한이 없습니다.',
}


def safe_code(code):
    return code if isinstance(code, str) and code in ERROR_MESSAGES else 'engine_operation_failed'


class ReplayFailure(ValueError):
    def __init__(self, code):
        self.code = safe_code(code)
        super().__init__(self.code)


def classify_diagnostic(value):
    """임시 문장을 보고 코드만 돌려준다. 문장은 남기지 않는다."""
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    text = str(value or '').lower()
    for status in (401, 403, 400):
        if re.search(r'\b(?:http(?:/\d(?:\.\d)?)?(?:\s+error)?|server returned|status(?:\s+code)?|error)\s*[:=]?\s*' + str(status) + r'\b', text):
            return 'http_' + str(status)
    if any(marker in text for marker in ('connection timed out', 'operation timed out', 'read timed out', 'connection timeout')):
        return 'network_timeout'
    if any(marker in text for marker in ('certificate verify failed', 'certificate verification failed', 'tls handshake', 'ssl handshake')):
        return 'tls_error'
    if any(marker in text for marker in ('not on whitelist', 'protocol not found', 'protocol not allowed')):
        return 'protocol_denied'
    if any(marker in text for marker in ('no space left on device', 'read-only file system', 'disk quota exceeded')):
        return 'storage_unavailable'
    if 'invalid data found when processing input' in text:
        return 'invalid_media'
    return 'engine_operation_failed'


def record_diagnostic(diagnostics, value):
    code = classify_diagnostic(value)
    if code != 'engine_operation_failed':
        diagnostics['code'] = code


def classify_exception(exc, diagnostics=None):
    if isinstance(exc, ReplayFailure):
        return exc.code
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    if type(status) is int and status in (400, 401, 403):
        return 'http_' + str(status)
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) or type(exc).__name__ in ('Timeout', 'ConnectTimeout', 'ReadTimeout', 'PoolTimeout', 'WriteTimeout'):
        return 'network_timeout'
    if isinstance(exc, ssl.SSLError) or type(exc).__name__ == 'SSLError':
        return 'tls_error'
    if isinstance(exc, FileNotFoundError):
        return 'tool_missing'
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EACCES):
        return 'storage_unavailable'
    if isinstance(exc, ValueError):
        code = {
            'quality_changed': 'quality_changed', 'invalid_media': 'invalid_media',
            'unsupported_media_container': 'invalid_media', 'single_valid_media_required': 'invalid_media',
            'ffprobe_missing': 'tool_missing', 'engine_missing': 'tool_missing',
            'unsafe_provider_url': 'protocol_denied', 'unsafe_provider_host': 'protocol_denied',
            'unsafe_command': 'protocol_denied', 'unexpected_tool': 'protocol_denied',
            'dedicated_empty_work_required': 'storage_unavailable', 'output_conflict': 'storage_unavailable',
        }.get(str(exc))
        if code:
            return code
    code = safe_code((diagnostics or {}).get('code'))
    if code != 'engine_operation_failed':
        return code
    # 예외 문장에는 서명된 주소나 헤더가 들어 있을 수 있어 메모리에서만 분류한다.
    return classify_diagnostic(str(exc))


class ReplayError(Exception):
    """API로 내보내는 오류: 상태 코드, 문장, 덧붙일 값."""

    def __init__(self, status, detail, extra=None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra or {}
