"""네이버 서명 HLS 전송."""
"""Offline fixtures for Naver signed-query media transport; no network."""
import ast
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch

from live_recorder.ext.downloads import hls_transport
from live_recorder.ext.downloads.errors import ReplayFailure
from live_recorder.ext.downloads.hls_transport import is_signed_naver_hls, rewrite_media_playlist, run_signed_hls

ROOT = Path(__file__).resolve().parents[1]
PARENT = 'https://vod.pstatic.net/root/playlist.m3u8?_lsu_sa_=SYNTHETIC%2BSECRET&policy=A%2FB&duplicate=parent'
PLAYLIST = b'#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\na.ts\n#EXT-X-ENDLIST\n'


class Response:
    status_code = 200
    headers = {}

    def __init__(self, content=PLAYLIST):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def iter_bytes(self, chunk_size):  # httpx로 바꾼 데 맞춘 이름
        yield self.content

    def iter_raw(self, chunk_size=None):  # 상한을 두고 풀어 읽는 httpread.iter_body가 쓴다
        yield self.content

    def iter_content(self, chunk_size):
        yield self.content


class HlsTransportTests(unittest.TestCase):
    def test_gate_leaves_progressive_dash_and_other_providers_unchanged(self):
        self.assertTrue(is_signed_naver_hls(PARENT))
        for url in ['https://vod.pstatic.net/a.mp4?_lsu_sa_=SYNTHETIC', 'https://vod.pstatic.net/a.m3u8',
                    'https://other.example/a.m3u8?_lsu_sa_=x', 'http://vod.pstatic.net/a?_lsu_sa_=x',
                    'https://u:p@vod.pstatic.net/a?_lsu_sa_=x', 'https://vod.pstatic.net:444/a?_lsu_sa_=x']:
            self.assertFalse(is_signed_naver_hls(url))

    def test_raw_query_added_only_to_same_origin_and_existing_values_win(self):
        playlist = '#EXTM3U\n#EXTINF:5,\na.ts?duplicate=child&own=keep\n#EXTINF:5,\nhttps://other.pstatic.net/b.ts?own=other\n#EXT-X-ENDLIST\n'
        rewritten = rewrite_media_playlist(playlist, PARENT).decode()
        self.assertIn('https://vod.pstatic.net/root/a.ts?duplicate=child&own=keep&_lsu_sa_=SYNTHETIC%2BSECRET&policy=A%2FB', rewritten)
        self.assertIn('https://other.pstatic.net/b.ts?own=other\n', rewritten)
        self.assertEqual(rewritten.count('SYNTHETIC'), 1)
        self.assertNotIn('duplicate=parent', rewritten)

    def test_existing_signed_key_is_preserved_and_not_duplicated(self):
        result = rewrite_media_playlist(PLAYLIST.replace(b'a.ts', b'a.ts?_lsu_sa_=CHILD&policy=CHILD'), PARENT)
        self.assertIn(b'_lsu_sa_=CHILD&policy=CHILD&duplicate=parent', result)
        self.assertNotIn(b'SYNTHETIC', result)
        self.assertEqual(result.count(b'_lsu_sa_'), 1)

    def test_key_and_map_uri_attributes_use_same_origin_rule(self):
        playlist = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin?own=1"\n#EXT-X-MAP:URI="https://other.pstatic.net/init.mp4?own=2"\n#EXTINF:5,\na.ts\n#EXT-X-ENDLIST\n'
        result = rewrite_media_playlist(playlist, PARENT).decode()
        self.assertIn('URI="https://vod.pstatic.net/root/key.bin?own=1&_lsu_sa_=SYNTHETIC%2BSECRET&policy=A%2FB&duplicate=parent"', result)
        self.assertIn('URI="https://other.pstatic.net/init.mp4?own=2"', result)

    def test_disallowed_media_origins_and_protocols_have_fixed_error(self):
        for uri in ['http://vod.pstatic.net/a.ts', 'https://127.0.0.1/a.ts', 'https://external.example/a.ts',
                    'file:///tmp/a.ts', 'https://u:p@vod.pstatic.net/a.ts',
                    'https://vod.pstatic.net:444/a.ts', 'https://vod.pstatic.net/a.ts#fragment']:
            with self.subTest(uri=uri), self.assertRaises(ReplayFailure) as error:
                rewrite_media_playlist(PLAYLIST.replace(b'a.ts', uri.encode()), PARENT)
            self.assertEqual(error.exception.code, 'protocol_denied')

    def test_master_live_empty_and_oversized_playlists_are_rejected(self):
        invalid = [b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchild.m3u8\n#EXT-X-ENDLIST\n',
                   PLAYLIST.replace(b'#EXT-X-ENDLIST\n', b''), b'#EXTM3U\n#EXT-X-ENDLIST\n', b'not HLS']
        for value in invalid:
            with self.assertRaises(ReplayFailure):
                rewrite_media_playlist(value, PARENT)
        with patch('live_recorder.ext.downloads.hls_transport.MAX_MANIFEST_BYTES', 10), self.assertRaises(ReplayFailure):
            rewrite_media_playlist(PLAYLIST, PARENT)
        with patch('live_recorder.ext.downloads.hls_transport.MAX_REWRITTEN_BYTES', 10), self.assertRaises(ReplayFailure):
            rewrite_media_playlist(PLAYLIST, PARENT)

    def test_malformed_key_uri_cannot_bypass_uri_validation(self):
        playlist = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=https://external.example/key\n#EXTINF:5,\na.ts\n#EXT-X-ENDLIST\n'
        with self.assertRaises(ReplayFailure) as error:
            rewrite_media_playlist(playlist, PARENT)
        self.assertEqual(error.exception.code, 'invalid_media')

    def test_native_failure_stderr_becomes_only_fixed_error_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / 'ffmpeg'
            tool.write_text('#!' + sys.executable + '\nimport sys\nsys.stderr.write("HTTP error 403 Forbidden token=SYNTHETIC Cookie=SYNTHETIC\\n")\nsys.exit(1)\n')
            tool.chmod(0o700)
            large = b'#EXTM3U\n' + b'#EXTINF:5,\na.ts\n' * 10000 + b'#EXT-X-ENDLIST\n'
            with patch('live_recorder.ext.downloads.hls_transport.httpx.stream', return_value=Response(large)), self.assertRaises(ReplayFailure) as error:
                run_signed_hls(PARENT, Path(tmp)/'out.mp4', None, str(tool), {}, subprocess, lambda p: None, lambda: None)
            self.assertEqual(error.exception.code, 'http_403')
            self.assertNotIn('SYNTHETIC', str(error.exception))

    def test_redirects_and_oversized_downloads_fail_before_native_process(self):
        redirect = Response()
        redirect.status_code = 302
        with patch('live_recorder.ext.downloads.hls_transport.httpx.stream', return_value=redirect), self.assertRaises(ReplayFailure) as error:
            run_signed_hls(PARENT, 'unused.mp4', None, 'ffmpeg', {}, subprocess, lambda p: None, lambda: None)
        self.assertEqual(error.exception.code, 'protocol_denied')
        with patch('live_recorder.ext.downloads.hls_transport.httpx.stream', return_value=Response()), patch('live_recorder.ext.downloads.hls_transport.MAX_MANIFEST_BYTES', 10), self.assertRaises(ReplayFailure):
            run_signed_hls(PARENT, 'unused.mp4', None, 'ffmpeg', {}, subprocess, lambda p: None, lambda: None)


class OpaqueSegmentCompatibilityTests(unittest.TestCase):
    def setUp(self):
        hls_transport._supports_extension_picky.cache_clear()

    def tearDown(self):
        hls_transport._supports_extension_picky.cache_clear()

    def run_fixture(self, playlist, *, opaque, supported=True, repeats=1):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)
            tool, marker = base/'ffmpeg', base/'capability-probes'
            code = """
import sys
from pathlib import Path
args=sys.argv[1:]
marker=Path(MARKER)
if args==['-hide_banner','-h','demuxer=hls']:
    marker.write_text((marker.read_text() if marker.exists() else '')+'probe\\n')
    print('  -extension_picky <boolean>' if SUPPORTED else 'HLS demuxer options:')
    raise SystemExit(0)
assert args[args.index('-protocol_whitelist')+1]=='https,tls,tcp,crypto,pipe'
assert args[args.index('-i')+1]=='pipe:0'
assert '-headers' not in args and '-cookies' not in args and '-tls_verify' not in args
assert not any('SYNTHETIC' in argument for argument in args)
assert ('-format_whitelist' in args)==OPAQUE
assert ('-extension_picky' in args)==(OPAQUE and SUPPORTED)
if OPAQUE:
    assert args[args.index('-format_whitelist')+1]=='hls,mpegts,mov,aac'
    assert args.index('-format_whitelist') < args.index('-i')
if OPAQUE and SUPPORTED:
    assert args[args.index('-extension_picky')+1]=='0'
    assert args.index('-extension_picky') < args.index('-i')
payload=sys.stdin.buffer.read()
assert b'_lsu_sa_=SYNTHETIC%2BSECRET' in payload
Path(args[-1]).write_bytes(b'FAKE-MEDIA'*200)
"""
            code=code.replace('MARKER',repr(str(marker))).replace('SUPPORTED',repr(supported)).replace('OPAQUE',repr(opaque))
            tool.write_text('#!'+sys.executable+'\n'+code)
            tool.chmod(0o700)
            registered=[]
            for index in range(repeats):
                with patch.object(hls_transport,'_fetch_manifest',return_value=playlist):
                    self.assertTrue(run_signed_hls(PARENT,base/('out-'+str(index)+'.mp4'),None,str(tool),
                        {'Cookie':'SYNTHETIC_COOKIE'},subprocess,registered.extend,registered.clear))
                self.assertFalse(registered)
            self.assertEqual(marker.read_text().count('probe') if marker.exists() else 0, 1 if opaque else 0)

    def test_normal_ts_and_m4s_do_not_change_command_or_probe_capability(self):
        for name in (b'a.ts?filename=opaque',b'a.m4s'):
            with self.subTest(name=name):self.run_fixture(PLAYLIST.replace(b'a.ts',name),opaque=False)

    def test_extensionless_media_adds_bounded_format_and_supported_extension_options(self):
        self.run_fixture(PLAYLIST.replace(b'a.ts',b'opaque/media?_lsu_sa_=SYNTHETIC%2BSECRET'),opaque=True,repeats=2)

    def test_extensionless_media_remains_compatible_with_old_ffmpeg_without_option(self):
        self.run_fixture(PLAYLIST.replace(b'a.ts',b'opaque/media'),opaque=True,supported=False)

    def test_opaque_initialization_or_key_alone_does_not_expand_normal_media_gate(self):
        playlist=PLAYLIST.replace(b'#EXTINF:5,',b'#EXT-X-MAP:URI="opaque/init"\n#EXT-X-KEY:METHOD=AES-128,URI="opaque/key"\n#EXTINF:5,')
        self.run_fixture(playlist,opaque=False)

    def test_all_uri_validation_precedes_opaque_capability_probe(self):
        playlist=PLAYLIST.replace(b'a.ts',b'opaque/media').replace(b'#EXTINF:5,',b'#EXT-X-KEY:METHOD=AES-128,URI="file:///forbidden"\n#EXTINF:5,')
        with patch.object(hls_transport,'_fetch_manifest',return_value=playlist), \
             patch.object(hls_transport,'_supports_extension_picky') as capability, \
             self.assertRaises(ReplayFailure) as error:
            run_signed_hls(PARENT,'unused.mp4',None,'ffmpeg',{},subprocess,lambda p:None,lambda:None)
        self.assertEqual(error.exception.code,'protocol_denied')
        capability.assert_not_called()

    def test_unicode_media_paths_classify_individually_without_ascii_assumption(self):
        for name,opaque in [('미디어.ts',False),('café.m4s',False),('미디어',True),('café',True)]:
            with self.subTest(name=name):
                self.run_fixture(PLAYLIST.replace(b'a.ts',name.encode('utf-8')),opaque=opaque)

    def test_empty_media_basename_does_not_expand_gate(self):
        payload=rewrite_media_playlist(PLAYLIST.replace(b'a.ts',b'/'),PARENT)
        self.assertFalse(hls_transport._has_extensionless_media(payload))

    def test_offline_capability_probe_failure_omits_unsupported_option(self):
        with patch.object(hls_transport.subprocess,'run',side_effect=subprocess.TimeoutExpired('ffmpeg',5)):
            self.assertFalse(hls_transport._supports_extension_picky('synthetic-unavailable'))


if __name__ == '__main__':
    unittest.main()
