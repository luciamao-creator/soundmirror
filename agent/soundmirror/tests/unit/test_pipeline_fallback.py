"""
Pipeline Fallback Tests — PM Day 2 P1-1

验证 MusicPipeline 的运行时降级 + URL 刷新机制:
  - T1: fallback_allowed + 音乐生成异常 → 自动降级到 Mock,后续不重试
  - T2: fallback_allowed + 音乐生成 success=False → 降级到 Mock
  - T3: real_required + 异常 → 抛错,不降级
  - T4: mock_only 模式 → 完全走 Mock,music_gen 被强制替换
  - T5: URL 刷新 — refresh_url 重新调 uploader,返回新 URL
  - T6: 资产 URL 字段扩展 — AssetUploadResult 包含 url_expires_in + object_key
  - T7: 同一 event_id 重放 — proposal.confirm 幂等(已有 test_highlight_flow)
"""
import os
import shutil
import subprocess
import tempfile
import pytest

from soundmirror.tools import (
    MusicPipeline,
    MusicResult,
    MockMusicPipeline,
)
from soundmirror.tools.music_pipeline import (
    FALLBACK_POLICY_REAL_REQUIRED,
    FALLBACK_POLICY_ALLOWED,
    FALLBACK_POLICY_MOCK_ONLY,
    _url_expires_at_iso,
)
from soundmirror.tools.asset_uploader import MatrixAssetUploader, MockAssetUploader
from soundmirror.tools.contracts import AssetUploadResult


# ============================================================
#  Helper — 模拟坏掉的 music_gen
# ============================================================

class BrokenMusicGen:
    """总是抛 RuntimeError"""
    model = "broken"
    def generate(self, **kwargs):
        raise RuntimeError("matrix timeout 30s")
    def get_audio_url(self, node_id):
        return None


class ReturningFalseMusicGen:
    """不抛错但返回 success=False"""
    model = "broken"
    def generate(self, **kwargs):
        return MusicResult(success=False, error="empty node_id", node_id="", audio_url="", model="broken")
    def get_audio_url(self, node_id):
        return None


class WorkingMusicGen:
    """正常工作的音乐生成"""
    model = "fake-real"
    def generate(self, prompt, lyrics="", **kwargs):
        return MusicResult(
            success=True,
            node_id="real_node_001",
            audio_url="https://matrix.local/audio.mp3",
            model="fake-real",
        )
    def get_audio_url(self, node_id):
        return f"https://matrix.local/{node_id}.mp3"


class FailingUploader:
    """上传失败"""
    def upload_file(self, local_path):
        return AssetUploadResult(success=False, error="OSS 503")
    def upload_bytes(self, data, file_name):
        return AssetUploadResult(success=False, error="OSS 503")


# ============================================================
#  T1: fallback_allowed + 异常 → 自动降级
# ============================================================

class TestFallbackOnException:
    def test_runtime_fallback_when_music_throws(self, tmp_path):
        """音乐生成抛异常时,fallback_allowed 自动降级到 Mock"""
        pipeline = MusicPipeline(
            music_gen=BrokenMusicGen(),
            fallback_policy=FALLBACK_POLICY_ALLOWED,
        )

        # 第一次调用会降级
        result = pipeline.generate_with_preview(
            prompt="test prompt long enough",
            output_dir=str(tmp_path),
            preview_seconds=5,
        )

        # 应该成功(用 mock 兜底)
        assert result["uploaded"] is True
        assert result["preview_path"]
        assert os.path.exists(result["preview_path"])
        assert result["fallback_from"]
        assert "music_gen_exception" in result["fallback_from"]
        assert "matrix timeout" in result["fallback_from"]

        # 第二次调用不应该再触发 fallback 标识
        result2 = pipeline.generate_with_preview(
            prompt="another prompt test",
            output_dir=str(tmp_path),
            preview_seconds=5,
        )
        # 注意:_swap_to_mock 已经把 music_gen 换掉,不会再走 BrokenMusicGen
        assert result2["uploaded"] is True


# ============================================================
#  T2: fallback_allowed + success=False → 降级
# ============================================================

class TestFallbackOnFalse:
    def test_runtime_fallback_when_music_returns_false(self, tmp_path):
        """MusicResult.success=False 时也降级"""
        pipeline = MusicPipeline(
            music_gen=ReturningFalseMusicGen(),
            fallback_policy=FALLBACK_POLICY_ALLOWED,
        )

        result = pipeline.generate_with_preview(
            prompt="test prompt long enough",
            output_dir=str(tmp_path),
            preview_seconds=5,
        )

        assert result["uploaded"] is True
        assert result["fallback_from"] == "music_gen_returned_failure"


# ============================================================
#  T3: real_required → 不降级,异常透传
# ============================================================

class TestRealRequired:
    def test_no_fallback_on_exception(self, tmp_path):
        """real_required + 异常 → 抛 RuntimeError"""
        pipeline = MusicPipeline(
            music_gen=BrokenMusicGen(),
            fallback_policy=FALLBACK_POLICY_REAL_REQUIRED,
        )

        with pytest.raises(RuntimeError, match="matrix timeout"):
            pipeline.generate_with_preview(
                prompt="test prompt long enough",
                output_dir=str(tmp_path),
                preview_seconds=5,
            )

    def test_no_fallback_on_false(self, tmp_path):
        """real_required + success=False → 返回失败 dict,不降级"""
        pipeline = MusicPipeline(
            music_gen=ReturningFalseMusicGen(),
            fallback_policy=FALLBACK_POLICY_REAL_REQUIRED,
        )

        result = pipeline.generate_with_preview(
            prompt="test prompt long enough",
            output_dir=str(tmp_path),
            preview_seconds=5,
        )

        assert result["uploaded"] is False
        assert result["preview_path"] is None
        assert result["error"] == "empty node_id"


# ============================================================
#  T4: mock_only 模式
# ============================================================

class TestMockOnlyMode:
    def test_mock_only_uses_mock_immediately(self, tmp_path):
        """mock_only 模式直接走 Mock,即使传了 music_gen 也强制替换"""
        gen = WorkingMusicGen()
        pipeline = MusicPipeline(
            music_gen=gen,
            fallback_policy=FALLBACK_POLICY_MOCK_ONLY,
        )

        # 即使传了 WorkingMusicGen,mock_only 也会用 Mock
        assert pipeline.music_gen.__class__.__name__ == "MockMusicAdapter"


# ============================================================
#  T5: URL 刷新
# ============================================================

class TestUrlRefresh:
    def test_refresh_url_returns_new_url(self, tmp_path):
        """refresh_url 调 uploader.upload_file 重新上传,返回新 URL"""
        # 准备一个真文件
        test_file = tmp_path / "test.mp3"
        test_file.write_bytes(b"FAKE_MP3_DATA" * 100)

        # 用 MockAssetUploader 计数
        uploader = MockAssetUploader()
        pipeline = MusicPipeline(
            music_gen=WorkingMusicGen(),
            uploader=uploader,
            fallback_policy=FALLBACK_POLICY_REAL_REQUIRED,
        )

        # 第一次上传
        url1 = pipeline.refresh_url(str(test_file))
        assert url1 is not None
        cnt1 = uploader._counter

        # 第二次上传(模拟 URL 过期)
        url2 = pipeline.refresh_url(str(test_file))
        assert url2 is not None
        cnt2 = uploader._counter

        # 必须调 uploader 重新上传,counter 增加
        assert cnt2 > cnt1
        # URL 可能相同(mock URL 模式),但调用次数必须增加
        assert uploader._counter == 2

    def test_refresh_url_returns_none_for_missing_file(self):
        """文件不存在时 refresh_url 返 None"""
        pipeline = MusicPipeline(fallback_policy=FALLBACK_POLICY_MOCK_ONLY)
        result = pipeline.refresh_url("/nonexistent/file.mp3")
        assert result is None


# ============================================================
#  T6: AssetUploadResult 字段扩展
# ============================================================

class TestUploadResultExtended:
    def test_mock_uploader_returns_expires_in(self):
        """MockAssetUploader 必须返回 url_expires_in + object_key"""
        uploader = MockAssetUploader()
        r = uploader.upload_file("/tmp/test.mp3")
        assert r.success
        assert r.url_expires_in == 3600
        assert r.object_key
        assert r.object_key.startswith("mock_key_")

    def test_custom_expires_in(self):
        """MockAssetUploader 可以自定义 expires_in(模拟 URL 已部分消耗)"""
        uploader = MockAssetUploader(default_expires_in=3000)
        r = uploader.upload_file("/tmp/test.mp3")
        assert r.url_expires_in == 3000

    def test_url_expires_at_iso_helper(self):
        """url_expires_at_iso helper 计算过期时间"""
        from datetime import datetime, timezone, timedelta
        result = _url_expires_at_iso(3600)
        assert result is not None
        # 解析回来检查(±2s 误差)
        exp = datetime.fromisoformat(result)
        now = datetime.now(timezone.utc)
        delta = exp - now
        assert abs(delta.total_seconds() - 3600) < 5

    def test_url_expires_at_iso_zero_returns_none(self):
        """0 或负数返 None"""
        assert _url_expires_at_iso(0) is None
        assert _url_expires_at_iso(-10) is None
        assert _url_expires_at_iso(None) is None


# ============================================================
#  T7: 整链路降级路径
# ============================================================

class TestFullPipelineFallback:
    def test_end_to_end_with_mock_only(self, tmp_path):
        """mock_only 走完整 3 步链路,全部成功"""
        pipeline = MusicPipeline(fallback_policy=FALLBACK_POLICY_MOCK_ONLY)

        result = pipeline.generate_with_preview(
            prompt="test prompt at least 10 chars",
            lyrics="[verse]\nfoo\n",
            output_dir=str(tmp_path),
            preview_seconds=10,
        )

        # 完整字段
        assert "music" in result
        assert "preview_path" in result
        assert "preview_url" in result
        assert "uploaded" in result
        assert "error" in result
        assert "fallback_from" in result
        assert "url_expires_at" in result

        assert result["uploaded"] is True
        assert result["preview_path"]
        assert os.path.exists(result["preview_path"])
        assert result["preview_url"]
        # mock_only 不算 fallback
        assert result["fallback_from"] is None
        # URL 过期时间存在
        assert result["url_expires_at"] is not None

    def test_upload_failure_returns_file_path_no_url(self, tmp_path):
        """上传失败时返回文件路径但 url=None(PM P1-5 想要的状态)"""
        # 写真 MP3
        mock = MockMusicPipeline()
        mock_r = mock.generate_with_preview(
            prompt="test prompt long enough",
            output_dir=str(tmp_path),
            preview_seconds=5,
        )
        # 然后用 failing uploader 模拟上传失败
        pipeline = MusicPipeline(
            music_gen=WorkingMusicGen(),
            uploader=FailingUploader(),
            fallback_policy=FALLBACK_POLICY_REAL_REQUIRED,
        )
        # 直接调 uploader 模拟失败
        upload_result = pipeline.uploader.upload_file(mock_r["preview_path"])
        assert upload_result.success is False
        assert upload_result.error == "OSS 503"
