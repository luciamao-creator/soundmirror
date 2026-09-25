"""
Mock Audio Fixtures Tests — PM Day 1 P1-4 + P1-5

把 `out/silent_mp3_test/case_01..07/` 的临时脚本永久化,并加失败注入测试:
  - 7 个参数组合 case
  - ffmpeg 不存在 / timeout / returncode != 0
  - 原子 rename 失败时清理 tmp
  - output 校验:文件大小 + ffprobe codec/duration/channels
"""
import json
import os
import shutil
import subprocess
import pytest

from soundmirror.tools.music_pipeline import (
    MockMusicPipeline,
)


# 直接调用 MockMusicPipeline._write_silent_mp3 静态方法
def _write(path, **kw):
    return MockMusicPipeline._write_silent_mp3(path, **kw)


# ============================================================
# Part A — 7 个参数组合 case
# ============================================================

@pytest.mark.parametrize(
    "case_id,duration,bitrate,channels,sample_rate,expected_min_size",
    [
        ("01_30s_stereo_128k_44.1khz", 30, 128000, 2, 44100, 470_000),
        ("02_5s_stereo_128k_44.1khz", 5, 128000, 2, 44100, 75_000),
        ("03_60s_stereo_128k_44.1khz", 60, 128000, 2, 44100, 940_000),
        ("04_30s_stereo_32k_44.1khz", 30, 32000, 2, 44100, 110_000),
        ("05_30s_stereo_256k_44.1khz", 30, 256000, 2, 44100, 940_000),
        ("06_30s_mono_64k_44.1khz", 30, 64000, 1, 44100, 230_000),
        ("07_30s_stereo_64k_22.05khz", 30, 64000, 2, 22050, 230_000),
    ],
)
def test_silent_mp3_case(tmp_path, case_id, duration, bitrate, channels, sample_rate, expected_min_size):
    """每个 case 必须:文件存在 + 大小符合预期 + ffprobe 校验"""
    out_path = tmp_path / f"{case_id}.mp3"

    size = _write(
        str(out_path),
        duration=duration,
        sample_rate=sample_rate,
        bitrate=bitrate,
        channels=channels,
    )

    # 1) 文件存在
    assert out_path.exists()
    # 2) 大小符合预期(允许 ±20%)
    assert size >= expected_min_size * 0.8, f"size {size} < {expected_min_size * 0.8}"
    assert size <= expected_min_size * 1.2, f"size {size} > {expected_min_size * 1.2}"

    # 3) ffprobe 验证 codec/duration/channels/sample_rate
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(out_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert probe.returncode == 0, f"ffprobe failed: {probe.stderr}"
    data = json.loads(probe.stdout)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    # codec
    assert any(s.get("codec_name") == "mp3" for s in streams), \
        f"codec != mp3: {[s.get('codec_name') for s in streams]}"
    # duration
    actual_dur = float(fmt.get("duration", 0))
    assert abs(actual_dur - duration) < 0.2, \
        f"duration mismatch: expected={duration} actual={actual_dur}"
    # channels
    if streams:
        assert streams[0].get("channels") == channels, \
            f"channels mismatch: expected={channels}"
        # sample_rate
        assert int(streams[0].get("sample_rate", 0)) == sample_rate, \
            f"sample_rate mismatch: expected={sample_rate}"


# ============================================================
# Part B — 故障注入(PM P1-4:ffmpeg 失败完整性保护)
# ============================================================

class TestFfmpegFailureHandling:
    def test_ffmpeg_not_installed_raises(self, tmp_path, monkeypatch):
        """ffmpeg 不在 PATH 时必须抛 RuntimeError"""
        # 把 PATH 清空
        monkeypatch.setenv("PATH", "/nonexistent")
        out = str(tmp_path / "nope.mp3")

        with pytest.raises(RuntimeError, match="ffmpeg not installed"):
            _write(out, duration=5)

    def test_ffmpeg_timeout_raises(self, tmp_path, monkeypatch):
        """ffmpeg 超时时必须抛 RuntimeError,且不留下半成品"""
        # mock subprocess.run 抛 TimeoutExpired
        import subprocess as sp
        def fake_run(*args, **kwargs):
            raise sp.TimeoutExpired(cmd="ffmpeg", timeout=0.01)
        monkeypatch.setattr(sp, "run", fake_run)

        out = str(tmp_path / "timeout.mp3")
        with pytest.raises(RuntimeError, match="ffmpeg timeout"):
            _write(out, duration=5)

        # 不应留下半成品
        assert not os.path.exists(out)
        assert not os.path.exists(out + ".tmp")

    def test_ffmpeg_nonzero_return_raises(self, tmp_path, monkeypatch):
        """ffmpeg 退出码 != 0 时必须抛 RuntimeError,不留下半成品"""
        import subprocess as sp
        class FakeProc:
            returncode = 1
            stderr = "fake stderr error"
        monkeypatch.setattr(sp, "run", lambda *a, **kw: FakeProc())

        out = str(tmp_path / "bad.mp3")
        with pytest.raises(RuntimeError, match="ffmpeg exit=1"):
            _write(out, duration=5)
        assert not os.path.exists(out)

    def test_ffprobe_codec_mismatch_raises(self, tmp_path, monkeypatch):
        """ffprobe 验证 codec != mp3 时必须抛 RuntimeError"""
        # 让 ffmpeg 假装成功,但 ffprobe 返回错误 codec
        import subprocess as sp
        call_count = {"n": 0}
        def fake_run(cmd, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # ffmpeg 调用 — 假装成功,写 1KB 假文件
                tmp_file = [a for a in cmd if a.endswith(".tmp")]
                if tmp_file:
                    open(tmp_file[0], "wb").write(b"\x00" * 1000)
                class P:
                    returncode = 0
                    stderr = ""
                return P()
            else:
                # ffprobe 调用 — 返回错误 codec
                class P:
                    returncode = 0
                    stdout = json.dumps({
                        "streams": [{"codec_name": "wav", "channels": 2}],
                        "format": {"duration": "5.0"},
                    })
                    stderr = ""
                return P()
        monkeypatch.setattr(sp, "run", fake_run)

        out = str(tmp_path / "wrongcodec.mp3")
        with pytest.raises(RuntimeError, match="codec != mp3"):
            _write(out, duration=5)
        assert not os.path.exists(out)

    def test_atomic_rename_cleanup(self, tmp_path):
        """正常成功路径:不留 .tmp"""
        out = str(tmp_path / "atomic.mp3")
        _write(out, duration=5)
        assert os.path.exists(out)
        assert not os.path.exists(out + ".tmp")

    def test_existing_file_overwritten(self, tmp_path):
        """目标文件已存在时,原子覆盖"""
        out = str(tmp_path / "overwrite.mp3")
        # 写一个旧文件
        with open(out, "wb") as f:
            f.write(b"OLD DATA")
        # 写真静音 MP3
        _write(out, duration=5)
        # 旧内容应被替换
        with open(out, "rb") as f:
            data = f.read(3)
        assert data != b"OLD"
        assert os.path.getsize(out) > 1000  # 真 MP3 不会 < 1KB


# ============================================================
# Part C — 全链路 generate_with_preview 测试
# ============================================================

class TestGenerateWithPreviewContract:
    def test_returns_correct_shape(self, tmp_path):
        """generate_with_preview 必须返回完整 dict"""
        p = MockMusicPipeline()
        result = p.generate_with_preview(
            prompt="test prompt at least 10 chars",
            lyrics="[verse]\nfoo\n",
            output_dir=str(tmp_path),
            preview_seconds=10,
        )
        # shape
        assert "music" in result
        assert "preview_path" in result
        assert "preview_url" in result
        assert "uploaded" in result
        assert "bytes" in result
        assert "error" in result
        # success
        assert result["music"].success is True
        assert result["music"].node_id.startswith("mock_node_")
        assert result["uploaded"] is True
        assert result["preview_path"]
        assert os.path.exists(result["preview_path"])

    def test_generated_counter_increments(self, tmp_path):
        """连续调用时 _counter 必须递增,node_id 必须唯一"""
        p = MockMusicPipeline()
        ids = []
        for _ in range(3):
            r = p.generate_with_preview(
                prompt="test prompt long enough",
                output_dir=str(tmp_path),
                preview_seconds=5,
            )
            ids.append(r["music"].node_id)
        # 3 个 node_id 必须唯一
        assert len(set(ids)) == 3
        # generated 列表 3 条
        assert len(p.generated) == 3
