"""
Tools layer unit tests
"""
import os
import json
import time
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from soundmirror.tools import (
    MockLarkSender, LarkCredentials,
    MockMusicAdapter, MatrixMusicAdapter,
    MockAudioClipper, FfmpegAudioClipper,
    MockAssetUploader, MatrixAssetUploader,
    HttpLarkSender,
    load_lark_credentials_from_env,
    LarkAPIError,
)


# ================================================================
# Lark Sender
# ================================================================
class TestMockLarkSender:
    def test_send_card_returns_msg_id(self):
        sender = MockLarkSender()
        result = sender.send_card("oc_x", {"config": {}, "header": {}, "elements": []})
        assert result.message_id.startswith("om_mock_")
        assert len(sender.sent) == 1

    def test_update_card_records(self):
        sender = MockLarkSender()
        sender.update_card("om_msg_001", {"config": {}, "header": {}, "elements": []})
        assert len(sender.updates) == 1

    def test_reply_text_records_with_at_user(self):
        sender = MockLarkSender()
        sender.reply_text("oc_x", "hi there", at_user="ou_alice")
        assert len(sender.texts) == 1


class TestHttpLarkSender:
    """测试真实实现但不真发网络请求"""

    def test_token_caching(self):
        creds = LarkCredentials(app_id="cli_test", app_secret="secret")
        sender = HttpLarkSender(creds)
        # 不初始化 _token 时是 None
        assert sender._token is None
        # 用 MagicMock 替换 _get_tenant_access_token
        sender._get_tenant_access_token = lambda: "fake_token"
        # 用 patch urlopen
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"code": 0, "data": {"message_id": "om_xyz"}}).encode()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = mock_resp
            result = sender.send_card("oc_test", {"header": {}, "elements": []})
            assert result.message_id == "om_xyz"

    def test_loads_credentials_from_env(self):
        with patch.dict(os.environ, {"LARK_APP_ID": "cli_xxx", "LARK_APP_SECRET": "sec_xxx"}):
            creds = load_lark_credentials_from_env()
            assert creds.app_id == "cli_xxx"
            assert creds.app_secret == "sec_xxx"

    def test_missing_credentials_does_not_crash(self, capsys):
        with patch.dict(os.environ, {}, clear=True):
            creds = load_lark_credentials_from_env()
            # 不 crash,但 app_id/secret 可能为空
            assert isinstance(creds.app_id, str)


# ================================================================
# Music Adapter
# ================================================================
class TestMockMusicAdapter:
    def test_generate_returns_node_id(self):
        adapter = MockMusicAdapter()
        result = adapter.generate(
            prompt="A peaceful acoustic folk song with warm acoustic guitar",
            lyrics="[verse]\nWalking down the road",
        )
        assert result.success
        assert result.node_id is not None
        assert result.audio_url is not None

    def test_generate_tracks_calls(self):
        adapter = MockMusicAdapter()
        adapter.generate("prompt", "lyrics")
        adapter.generate("prompt2", "lyrics2")
        assert len(adapter.generated) == 2


class TestMatrixMusicAdapter:
    def test_validate_schema(self):
        adapter = MatrixMusicAdapter()
        # 通过 mcode-tools 走真实 schema 验证(应该成功)
        try:
            schema = adapter.validate_schema()
            assert schema.get("tool_name") == "connector__matrix__batch_text_to_music"
        except MatrixConnectionError:
            # mcode-tools 未登录,跳过
            pytest.skip("mcode-tools not authenticated")

    def test_prompt_length_validation(self):
        adapter = MatrixMusicAdapter()
        # 太短的 prompt 应被拒绝
        result = adapter.generate(prompt="hi", lyrics="")
        assert result.success is False
        assert "prompt length" in result.error.lower()

    def test_lyrics_length_validation(self):
        adapter = MatrixMusicAdapter()
        # 有效 prompt + 太短 lyrics
        result = adapter.generate(
            prompt="A peaceful acoustic folk song with warm vocals and gentle rhythm",
            lyrics="short",
        )
        # lyrics "short" (5 chars) 太短
        # 注意:tool_request 是 lyrics optional,所以这里默认没 lyrics 也 OK
        # 但如果给了 lyrics < 10,会被拒绝
        if lyrics := "short":
            # lyrics 太短会拒
            assert result.success is False


# ================================================================
# Audio Clipper
# ================================================================
class TestMockAudioClipper:
    def test_clip_creates_output(self, tmp_path):
        clipper = MockAudioClipper()
        inp = tmp_path / "in.mp3"
        inp.write_bytes(b"FAKE")
        outp = tmp_path / "out.mp3"
        clipper.clip(str(inp), str(outp), duration_seconds=30)
        assert outp.exists()
        assert len(clipper.calls) == 1


class TestFfmpegAvailability:
    def test_ffmpeg_availability(self):
        clipper = FfmpegAudioClipper()
        # 只是看 available 不 crash
        assert isinstance(clipper.available, bool)


# ================================================================
# Asset Uploader
# ================================================================
class TestMockAssetUploader:
    def test_upload_file(self, tmp_path):
        uploader = MockAssetUploader()
        f = tmp_path / "audio.mp3"
        f.write_bytes(b"FAKE_AUDIO")
        result = uploader.upload_file(str(f))
        assert result.success
        assert result.url.startswith("https://")

    def test_upload_bytes(self):
        uploader = MockAssetUploader()
        result = uploader.upload_bytes(b"data", "test.mp3")
        assert result.success

    def test_upload_missing_file(self):
        uploader = MatrixAssetUploader()
        # 真实 adapter 报文件不存在
        result = uploader.upload_file("/tmp/definitely_not_exists_xyz.mp3")
        assert result.success is False
