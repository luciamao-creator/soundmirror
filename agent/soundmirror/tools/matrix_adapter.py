"""
Matrix Music Adapter — 包装 mcode-tools Matrix connector

参考 M0C_MAVIS_SPIKE_REPORT 验证的合约:
  - tool_name: connector__matrix__batch_text_to_music
  - requests[]: {prompt (required, 10-300 chars), lyrics (optional, 10-600),
                 format [mp3|wav], bitrate, sample_rate}
  - 输出: success_items[].node_id + file_name
  - 用 get_asset_url <node_id> 拿 2-day signed URL
  - 注: 不返回 model 名称,记录为 unknown

Idempotency:
  - 同一 idempotency_key 重复调用通过 Orchestrator 层 dedup 保证
  - 这里只负责实际调用 Matrix

错误处理:
  - subprocess timeout > 60s → MusicResult(success=False, error="timeout")
  - matrix 拒绝 / 失败 → MusicResult(success=False, error=matrix.error)
"""
import json
import subprocess
import time
from pathlib import Path
from typing import Optional
from .contracts import MusicAdapter, MusicResult


MATRIX_MUSIC_TOOL = "connector__matrix__batch_text_to_music"
MATRIX_TTS_TOOL = "connector__matrix__batch_text_to_audio"
MATRIX_LISTEN_TOOL = "connector__matrix__listen_audio"

# 默认生成参数(M0C 验证可用)
DEFAULT_BITRATE = 128000
DEFAULT_SAMPLE_RATE = 44100
DEFAULT_FORMAT = "mp3"


class MatrixMusicAdapter:
    """通过 mcode-tools subprocess 调 Matrix connector"""

    def __init__(self, mcode_bin: str = "mcode-tools", timeout_seconds: int = 90):
        self.mcode_bin = mcode_bin
        self.timeout = timeout_seconds
        # tool_name 缓存(M0C schema version)
        self._validated = False

    def validate_schema(self) -> dict:
        """检查 tool schema 是否一致(M0C spike 验证 schema 已稳定)"""
        if self._validated:
            return {"validated": True}
        result = subprocess.run(
            [self.mcode_bin, "connector", "tool", MATRIX_MUSIC_TOOL],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise MatrixConnectionError(f"Cannot fetch tool schema: {result.stderr}")
        schema = json.loads(result.stdout)
        self._validated = True
        return schema

    def generate(
        self,
        prompt: str,
        lyrics: str = "",
        bitrate: int = DEFAULT_BITRATE,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        format: str = DEFAULT_FORMAT,
        **kwargs,
    ) -> MusicResult:
        """调用 Matrix 一次生成

        Args:
            prompt: music style description (10-300 chars)
            lyrics: 歌词 (10-600 chars, optional for instrumental)
            bitrate: 32000 / 64000 / 128000 / 256000
            sample_rate: 16000 / 24000 / 32000 / 44100
            format: mp3 / wav

        Returns:
            MusicResult { success, node_id, file_name, audio_url, error }
        """
        # 校验 prompt 长度
        if len(prompt) < 10 or len(prompt) > 300:
            return MusicResult(success=False, error=f"prompt length {len(prompt)} out of range [10, 300]")

        request_item = {
            "prompt": prompt,
            "format": format,
            "bitrate": bitrate,
            "sample_rate": sample_rate,
        }
        if lyrics:
            if len(lyrics) < 10 or len(lyrics) > 600:
                return MusicResult(success=False, error=f"lyrics length {len(lyrics)} out of range [10, 600]")
            request_item["lyrics"] = lyrics

        args = {
            "requests": [request_item],
        }
        args_str = json.dumps(args, ensure_ascii=False)

        # 调用 mcode-tools
        try:
            proc = subprocess.run(
                [self.mcode_bin, "connector", "call", MATRIX_MUSIC_TOOL, "--args", args_str],
                capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return MusicResult(success=False, error=f"matrix call timeout after {self.timeout}s")

        if proc.returncode != 0:
            return MusicResult(
                success=False,
                error=f"matrix process failed: {proc.stderr or proc.stdout}",
            )

        # 解析输出
        try:
            response = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            return MusicResult(success=False, error=f"matrix returned invalid JSON: {e}")

        # 检查 success_items
        success_items = response.get("success_items", [])
        if not success_items:
            # 可能是 failed_items 或 error
            failed_items = response.get("failed_items", [])
            err = response.get("error") or response.get("msg")
            return MusicResult(
                success=False,
                error=f"matrix returned no success_items: failed={failed_items}, error={err}",
                raw_response=response,
            )

        item = success_items[0]
        node_id = item.get("node_id")
        file_name = item.get("file_name")

        if not node_id:
            return MusicResult(
                success=False,
                error="missing node_id in success_items",
                raw_response=response,
            )

        # 短期签名 URL(2 天)
        audio_url = self.get_audio_url(node_id)

        return MusicResult(
            success=True,
            node_id=node_id,
            file_name=file_name,
            audio_url=audio_url,
            model="unknown",
            raw_response=response,
        )

    def get_audio_url(self, node_id: str) -> Optional[str]:
        """从 node_id 拿短期 URL

        2026-09-19 改:Matrix 实际返回 `download_url` 字段(不是 `url`)
        """
        try:
            proc = subprocess.run(
                [self.mcode_bin, "get_asset_url", node_id],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
            # 兼容多种字段名(M0C 时是 `url`,最新版本是 `download_url`)
            return (
                data.get("url")
                or data.get("audio_url")
                or data.get("download_url")   # ← 实际 Matrix 返回这个
                or proc.stdout.strip()
            )
        except json.JSONDecodeError:
            return proc.stdout.strip() or None

    def transcribe(self, audio_url: str) -> Optional[str]:
        """用 listen_audio 转写音频(M0C:返回 LLM 文本,无时间戳)"""
        args = json.dumps({"url": audio_url})
        try:
            proc = subprocess.run(
                [self.mcode_bin, "connector", "call", MATRIX_LISTEN_TOOL, "--args", args],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
            return data.get("text") or data.get("transcript") or str(data)
        except json.JSONDecodeError:
            return proc.stdout


class MatrixConnectionError(Exception):
    pass


class MockMusicAdapter:
    """Memory 假生成器(测试用,不用真调 Matrix)"""

    def __init__(self):
        self.generated = []  # 调用记录
        self._counter = 0

    def generate(self, prompt, lyrics="", **kwargs):
        self._counter += 1
        nid = f"mock_node_{self._counter:08d}"
        self.generated.append({"node_id": nid, "prompt": prompt[:50], "lyrics_len": len(lyrics)})
        return MusicResult(
            success=True,
            node_id=nid,
            file_name="mock.mp3",
            audio_url=f"https://mock.local/{nid}.mp3",
            model="mock-model-v0",
        )

    def get_audio_url(self, node_id):
        return f"https://mock.local/{node_id}.mp3"
