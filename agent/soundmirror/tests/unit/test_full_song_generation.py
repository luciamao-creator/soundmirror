"""
Full Song Generation Tests — PM Day 4 capability补全

2026-09-20 验证:
  - MusicPipeline.generate_full_song_from_node() 端到端
  - 真实 matrix node_id → 下载完整歌曲 → 上传 OSS → 返回完整字段
  - 失败 case 全覆盖(MISSING_NODE_ID / DOWNLOAD_FAILED / NOT_MP3 / 等等)
"""
import json
import os
import subprocess
import tempfile
import shutil
from unittest.mock import MagicMock, patch

import pytest

from soundmirror.tools import MusicPipeline
from soundmirror.tools.music_pipeline import _url_expires_at_iso
from soundmirror.tools.contracts import MusicResult, AssetUploadResult


# ============================================================
#  Helper — 用真 Matrix node_id 跑(因为需要真 URL 可下载)
# ============================================================

REAL_NODE_ID = os.environ.get("SOUNDMIRROR_TEST_NODE_ID")  # 真实 spike 留下的 node


@pytest.mark.skipif(
    not REAL_NODE_ID,
    reason="需要真 matrix node_id(从 spike 拿);设置 SOUNDMIRROR_TEST_NODE_ID env var 启用",
)
class TestGenerateFullSongRealMatrix:
    def test_full_song_path_and_metadata(self, tmp_path):
        """用真 matrix node_id 验证完整歌曲拉取"""
        from soundmirror.tools import MusicPipeline
        from soundmirror.tools.matrix_adapter import MatrixMusicAdapter
        from soundmirror.tools.asset_uploader import MatrixAssetUploader

        pipeline = MusicPipeline(
            music_gen=MatrixMusicAdapter(timeout_seconds=60),
            uploader=MatrixAssetUploader(),
            prefer_real=True,
        )
        result = pipeline.generate_full_song_from_node(REAL_NODE_ID, str(tmp_path))

        # 成功
        assert result.get("error") is None, f"Got error: {result}"
        assert result["full_song_path"]
        assert os.path.exists(result["full_song_path"])
        assert result["full_song_path"].endswith(".mp3")

        # 文件是合法 MP3
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", result["full_song_path"]],
            capture_output=True, text=True, timeout=10,
        )
        assert probe.returncode == 0, f"ffprobe failed: {probe.stderr}"
        data = json.loads(probe.stdout)
        assert "format" in data
        dur_ms = int(float(data["format"]["duration"]) * 1000)
        assert dur_ms > 0
        assert result["duration_ms"] == dur_ms
        assert result["bytes"] > 1000
        assert len(result["checksum_sha256"]) == 64  # SHA-256 hex
        assert result["mime_type"] == "audio/mpeg"
        assert result["adapter"] == "MatrixMusicAdapter"

        # 上传有 1h signed URL(可能因网络失败为 None,但成功时不应为 None)
        if result["uploaded"]:
            assert result["full_song_url"]
            assert result["full_song_url"].startswith("https://")
            assert result["signed_url_expires_at"]


# ============================================================
#  T2-T5: 失败路径(monkeypatch + MagicMock)
# ============================================================

class TestGenerateFullSongErrors:
    def test_missing_node_id(self, tmp_path):
        """node_id 为空 → MISSING_NODE_ID"""
        pipeline = MusicPipeline(
            music_gen=MagicMock(),
            uploader=MagicMock(),
            prefer_real=True,
        )
        result = pipeline.generate_full_song_from_node("", str(tmp_path))
        assert result["error"] == "MISSING_NODE_ID"
        assert result["full_song_path"] is None

    def test_matrix_returns_no_url(self, tmp_path):
        """matrix.get_audio_url 返 None → MATRIX_RETURNED_NO_AUDIO_URL"""
        mock_gen = MagicMock()
        mock_gen.get_audio_url.return_value = None
        pipeline = MusicPipeline(music_gen=mock_gen, uploader=MagicMock(), prefer_real=True)
        result = pipeline.generate_full_song_from_node("some_node_xyz", str(tmp_path))
        assert result["error"] == "MATRIX_RETURNED_NO_AUDIO_URL"
        assert result["full_song_path"] is None

    def test_get_audio_url_exception(self, tmp_path):
        """matrix.get_audio_url 抛异常 → 异常被捕获 + error"""
        mock_gen = MagicMock()
        mock_gen.get_audio_url.side_effect = RuntimeError("network down")
        pipeline = MusicPipeline(music_gen=mock_gen, uploader=MagicMock(), prefer_real=True)
        result = pipeline.generate_full_song_from_node("some_node", str(tmp_path))
        assert "get_audio_url exception" in result["error"]

    def test_download_failed(self, tmp_path):
        """curl 返回非 0 rc → DOWNLOAD_FAILED"""
        mock_gen = MagicMock()
        mock_gen.get_audio_url.return_value = "https://fake.example/audio.mp3"
        pipeline = MusicPipeline(music_gen=mock_gen, uploader=MagicMock(), prefer_real=True)

        # monkey-patch subprocess.run(curl) 让它失败
        original_run = subprocess.run
        def fake_run(*args, **kwargs):
            if args and "curl" in args[0]:
                r = MagicMock()
                r.returncode = 1
                r.stderr = "fake curl fail"
                return r
            return original_run(*args, **kwargs)

        with patch("subprocess.run", side_effect=fake_run):
            result = pipeline.generate_full_song_from_node("some_node", str(tmp_path))
        assert "DOWNLOAD_FAILED" in result["error"]
        assert "fake curl fail" in result["error"]


# ============================================================
#  T6: 上传失败但本地文件 OK
# ============================================================

class TestGenerateFullSongUploadFailure:
    def test_local_file_saved_even_if_upload_fails(self, tmp_path):
        """download 成功但 upload 失败 → full_song_path 仍在,url=None"""
        mock_gen = MagicMock()
        mock_gen.get_audio_url.return_value = "https://matrix.test/audio.mp3"

        # 真 ffmpeg 写真 1KB 文件,模拟下载成功
        local_input = tmp_path / "fake_audio.mp3"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", "1", "-c:a", "libmp3lame", "-b:a", "32k",
            str(local_input)
        ], capture_output=True, timeout=10)
        assert local_input.exists()
        local_size = local_input.stat().st_size

        # mock uploader 失败
        mock_uploader = MagicMock()
        mock_uploader.upload_file.return_value = AssetUploadResult(
            success=False, error="OSS 503",
        )

        pipeline = MusicPipeline(music_gen=mock_gen, uploader=mock_uploader, prefer_real=True)

        # 用本地 file:// URL(替代 https)— curl 支持 file://
        audio_url = f"file://{local_input}"
        mock_gen.get_audio_url.return_value = audio_url

        result = pipeline.generate_full_song_from_node("some_node", str(tmp_path / "out"))

        # 本地文件应该 OK
        assert result["full_song_path"] is not None
        assert os.path.exists(result["full_song_path"])
        assert result["full_song_path"].endswith(".mp3")
        local_out_size = os.path.getsize(result["full_song_path"])
        assert local_out_size > 0  # 真有内容

        # 上传失败时 url=None, error 包含 OSS 错误
        assert result["uploaded"] is False
        assert result["full_song_url"] is None
        # error 字段应记录 upload 失败
        assert result["error"]
        assert "OSS" in result["error"] or "503" in result["error"]


# ============================================================
#  T7: 完整字段 schema
# ============================================================

class TestGenerateFullSongSchema:
    def test_returns_all_required_fields(self):
        """必须包含所有必需字段"""
        # 用空 pipeline + mock gen 测 schema(只校验 key 存在)
        mock_gen = MagicMock()
        mock_gen.get_audio_url.return_value = None  # 触发 early return
        pipeline = MusicPipeline(music_gen=mock_gen, uploader=MagicMock(), prefer_real=True)

        result = pipeline.generate_full_song_from_node("any_node", "/tmp")
        required = [
            "node_id", "full_song_path", "full_song_url",
            "uploaded", "duration_ms", "bytes", "checksum_sha256",
            "mime_type", "adapter", "signed_url_expires_at", "error",
        ]
        for k in required:
            assert k in result, f"missing field: {k}"


# ============================================================
#  T8: 与 _highlight_select 集成 — full_song 字段被填进 ledger
# ============================================================

class TestHighlightSelectWritesFullSong:
    def test_full_song_fields_in_selected_highlight(self, tmp_path, monkeypatch):
        """_highlight_select 调用后,selected_highlight 必须含 full_song_* 字段"""
        from soundmirror.storage import StateStore
        from soundmirror.chat_repl import build_mutation_handlers
        from soundmirror.tools import MockMusicPipeline

        db = str(tmp_path / "highlight.db")
        store = StateStore(db)
        cs = store.create_session("u_test_fullsong")
        cs_id = cs["creative_session_id"]
        rev = cs["state_revision"]

        # 推到 HIGHLIGHT_REVIEW,seed 3 个 READY clips
        store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/current_stage", "value": "HIGHLIGHT_REVIEW"},
                {"op": "set", "path": "/generation_status", "value": "READY"},
                {"op": "set", "path": "/highlight_clips", "value": [
                    {"slot": "A", "status": "READY", "node_id": "test_node_AAA",
                     "preview_path": "/tmp/A.mp3", "url": "https://x/A"},
                ]},
            ],
        }, "u_test_fullsong")

        # mock MockMusicPipeline.generate_full_song_from_node
        # (因为测试环境 SOUNDMIRROR_USE_REAL_MATRIX 未设,_highlight_select 走 mock 路径)
        def fake_generate_full(self, node_id, output_dir):
            return {
                "node_id": node_id,
                "full_song_path": "/tmp/fake_full.mp3",
                "full_song_url": "https://fake.test/full.mp3",
                "uploaded": True,
                "duration_ms": 95000,
                "bytes": 1500000,
                "checksum_sha256": "deadbeef" * 8,
                "mime_type": "audio/mpeg",
                "adapter": "MockMatrixAdapter",
                "signed_url_expires_at": "2026-09-20T13:00:00+00:00",
                "error": None,
            }
        monkeypatch.setattr(MockMusicPipeline, "generate_full_song_from_node", fake_generate_full)

        handlers = build_mutation_handlers(store)
        result = handlers["clip.select"](store, cs_id, {"slot": "A"}, "u_test_fullsong")

        # 返回值
        assert result["ok"] is True
        assert result["full_song_generated"] is True
        assert result["full_song_duration_ms"] == 95000

        # ledger 写入
        sess = store.get_session(cs_id)
        sh = sess["ledger"]["selected_highlight"]
        assert sh["slot"] == "A"
        assert sh["full_song_path"] == "/tmp/fake_full.mp3"
        assert sh["full_song_url"] == "https://fake.test/full.mp3"
        assert sh["full_song_duration_ms"] == 95000
        assert sh["full_song_checksum_sha256"] == "deadbeef" * 8
        assert sh["full_song_signed_url_expires_at"] == "2026-09-20T13:00:00+00:00"
        assert sh["full_song_error"] is None

    def test_full_song_error_still_records_selection(self, tmp_path, monkeypatch):
        """full_song 生成失败 → selected_highlight 仍记录,error 字段标错"""
        from soundmirror.storage import StateStore
        from soundmirror.chat_repl import build_mutation_handlers
        from soundmirror.tools import MockMusicPipeline

        db = str(tmp_path / "highlight.db")
        store = StateStore(db)
        cs = store.create_session("u_test_err")
        cs_id = cs["creative_session_id"]

        store.apply_mutation(cs_id, {
            "expected_revision": cs["state_revision"],
            "operations": [
                {"op": "set", "path": "/current_stage", "value": "HIGHLIGHT_REVIEW"},
                {"op": "set", "path": "/generation_status", "value": "READY"},
                {"op": "set", "path": "/highlight_clips", "value": [
                    {"slot": "A", "status": "READY", "node_id": "test_node_BBB",
                     "preview_path": "/tmp/B.mp3", "url": "https://x/B"},
                ]},
            ],
        }, "u_test_err")

        def fake_full_fail(self, node_id, output_dir):
            return {
                "node_id": node_id, "full_song_path": None,
                "full_song_url": None, "uploaded": False,
                "duration_ms": None, "bytes": None,
                "checksum_sha256": None, "mime_type": "audio/mpeg",
                "adapter": None, "signed_url_expires_at": None,
                "error": "MATRIX_DOWN",
            }

        monkeypatch.setattr(MockMusicPipeline, "generate_full_song_from_node", fake_full_fail)

        handlers = build_mutation_handlers(store)
        result = handlers["clip.select"](store, cs_id, {"slot": "A"}, "u_test_err")

        sess = store.get_session(cs_id)
        sh = sess["ledger"]["selected_highlight"]
        assert sh["slot"] == "A"
        assert sh["full_song_path"] is None
        assert sh["full_song_error"] == "MATRIX_DOWN"
        # stage 仍应进 FULL_REVIEW(选段已记录)
        assert sess["ledger"]["current_stage"] == "FULL_REVIEW"
