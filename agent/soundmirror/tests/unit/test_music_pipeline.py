"""
Music Pipeline Tests
"""
import os
import pytest
import tempfile
import shutil

from soundmirror.tools import (
    MusicPipeline,
    MockMusicPipeline,
    MusicResult,
)


class TestMockMusicPipeline:
    def test_mock_pipeline_full_flow(self, tmp_path):
        pipeline = MockMusicPipeline()
        result = pipeline.generate_with_preview(
            prompt="An acoustic folk ballad with warm vocals",
            lyrics="[verse]\nTest lyrics here\n",
            output_dir=str(tmp_path),
            preview_seconds=30,
        )

        # Music generated
        assert result["music"].success
        assert result["music"].node_id
        assert result["music"].audio_url

        # Preview created
        assert result["preview_path"]
        assert os.path.exists(result["preview_path"])
        assert os.path.getsize(result["preview_path"]) > 0

        # Upload result (mock returns URL)
        assert result["uploaded"] is True
        assert result["preview_url"]

        # Mock tracks calls
        assert len(pipeline.generated) == 1
        assert pipeline.generated[0]["node_id"] == result["music"].node_id


class TestRealMusicPipeline:
    def test_real_pipeline_instantiates(self):
        """真实 pipeline 构造函数不应该 crash"""
        # 不实际调用 matrix(避免 30s+ timeout)
        pipeline = MusicPipeline(prefer_real=True)
        assert pipeline.music_gen is not None
        assert pipeline.clipper is not None
        assert pipeline.uploader is not None

    def test_real_pipeline_components_valid(self):
        """MusicPipeline 实际选择的实现都满足 contract"""
        pipeline = MusicPipeline(prefer_real=True)
        # 至少有 generate / clip / upload 三个能力
        assert hasattr(pipeline.music_gen, "generate")
        assert hasattr(pipeline.clipper, "clip_from_url")
        assert hasattr(pipeline.uploader, "upload_file")


class TestPipelineContract:
    """所有 pipeline 实现都遵守同一 contract"""

    def _check_contract(self, pipeline, tmp_path):
        result = pipeline.generate_with_preview(
            prompt="Test prompt at least 10 chars",
            lyrics="",
            output_dir=str(tmp_path),
        )
        # 必须返回这些字段
        assert "music" in result
        assert "preview_path" in result
        assert "preview_url" in result
        assert "uploaded" in result
        assert "error" in result
        # music 必有 success + node_id
        if result["music"].success:
            assert result["music"].node_id
            assert result["music"].audio_url

    def test_mock_contract(self, tmp_path):
        self._check_contract(MockMusicPipeline(), tmp_path)
