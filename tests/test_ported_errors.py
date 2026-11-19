"""다운로드 오류 코드와 진단 문장 다루기."""
"""Safe error protocol, including actual native stderr; no provider requests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

from live_recorder.ext.downloads.errors import ERROR_MESSAGES, ReplayFailure, classify_diagnostic, classify_exception, safe_code

ROOT = Path(__file__).resolve().parents[1]


class ErrorTests(unittest.TestCase):
    def test_exception_codes_are_fixed(self):
        self.assertEqual(classify_exception(ReplayFailure('quality_changed')), 'quality_changed')
        self.assertEqual(classify_exception(TimeoutError()), 'network_timeout')
        self.assertEqual(classify_exception(FileNotFoundError()), 'tool_missing')
        self.assertEqual(classify_exception(ValueError('invalid_media')), 'invalid_media')
        self.assertEqual(safe_code('RAW_SECRET_URL'), 'engine_operation_failed')
        self.assertEqual(classify_exception(Exception('unclassified synthetic detail')), 'engine_operation_failed')
        self.assertNotIn('설정', ERROR_MESSAGES['engine_operation_failed'])

    def test_native_diagnostic_classification(self):
        for message, code in [('Server returned 400 Bad Request', 'http_400'), ('HTTP error 401 Unauthorized', 'http_401'),
                              ('HTTP/1.1 403 Forbidden', 'http_403'), ('Connection timed out', 'network_timeout'),
                              ('certificate verify failed', 'tls_error'), ('Protocol https not on whitelist', 'protocol_denied'),
                              ('No space left on device', 'storage_unavailable')]:
            self.assertEqual(classify_diagnostic(message), code)
