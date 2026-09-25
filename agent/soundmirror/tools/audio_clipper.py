"""
Audio Clipper — ffmpeg 包装,30 秒预览剪裁

PRD V1.3 要求:
  - 给用户 30 秒预览,不全曲
  - 从全曲的副歌或钩子 start 处开始剪
  - 输出 mp3 / wav 都能播放

实现:
  - ffmpeg 在 sandbox 可能没有,必须 graceful fallback
  - 用 subprocess check
"""
import os
import subprocess
import shutil
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional


class FfmpegAudioClipper:
    """依赖 ffmpeg 在 PATH 中"""

    def __init__(self, ffmpeg_bin: str = "ffmpeg"):
        self.ffmpeg_bin = ffmpeg_bin
        # 检查 ffmpeg 是否可用
        self._available = shutil.which(self.ffmpeg_bin) is not None

    @property
    def available(self) -> bool:
        return self._available

    def clip(
        self,
        input_path: str,
        output_path: str,
        duration_seconds: int = 30,
        start_seconds: int = 0,
    ) -> bool:
        """本地文件 → 30s 剪裁

        ffmpeg -ss {start} -i {input} -t {duration} -c:a copy {output}
        """
        if not self._available:
            raise RuntimeError("ffmpeg not in PATH; install or pass a different clipper")
        if not Path(input_path).exists():
            raise FileNotFoundError(input_path)

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            self.ffmpeg_bin,
            "-y",                  # overwrite
            "-ss", str(start_seconds),
            "-i", input_path,
            "-t", str(duration_seconds),
            "-c:a", "copy",        # 不重新编码,保留原格式
            "-vn",
            output_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return proc.returncode == 0 and Path(output_path).exists()

    def clip_from_url(
        self,
        audio_url: str,
        output_path: str,
        duration_seconds: int = 30,
        start_seconds: int = 0,
    ) -> bool:
        """远程 URL → 30s 剪裁

        2026-09-19 改:先下载到本地 tmp,再 clip。
        之前:直接 ffmpeg 流式读 OSS 签名 URL,但 MP3 `-c:a copy` + `-ss` seek 不可靠
        现在:curl/wget → tmp.mp3 → ffmpeg clip(tmp.mp3 → output)
        """
        if not self._available:
            raise RuntimeError("ffmpeg not in PATH")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        import tempfile
        import subprocess as sp

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmpf:
            tmp_in = tmpf.name

        try:
            # 下载(curl 处理 HTTPS 签名 URL 最稳)
            dl = sp.run(
                ["curl", "-sSL", "--max-time", "60", "-o", tmp_in, audio_url],
                capture_output=True, text=True, timeout=70,
            )
            if dl.returncode != 0 or not Path(tmp_in).exists():
                return False
            if Path(tmp_in).stat().st_size < 1000:
                return False

            # 剪裁
            cmd = [
                self.ffmpeg_bin,
                "-y",
                "-ss", str(start_seconds),
                "-i", tmp_in,
                "-t", str(duration_seconds),
                "-c:a", "copy",
                "-vn",
                output_path,
            ]
            proc = sp.run(cmd, capture_output=True, text=True, timeout=30)
            ok = proc.returncode == 0 and Path(output_path).exists()
            return ok
        finally:
            try:
                Path(tmp_in).unlink()
            except OSError:
                pass


class MockAudioClipper:
    """测试用:不实际剪裁,只标记"""

    def __init__(self):
        self.calls = []

    def clip(self, input_path, output_path, duration_seconds=30, start_seconds=0):
        self.calls.append({
            "method": "clip", "input": input_path, "output": output_path,
            "duration": duration_seconds, "start": start_seconds,
        })
        # 模拟写一个伪文件
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"MOCK_AUDIO_30s")
        return True

    def clip_from_url(self, audio_url, output_path, duration_seconds=30, start_seconds=0):
        self.calls.append({
            "method": "clip_from_url", "url": audio_url, "output": output_path,
            "duration": duration_seconds, "start": start_seconds,
        })
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"MOCK_AUDIO_30s_FROM_URL")
        return True
