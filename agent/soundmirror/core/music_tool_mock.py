"""Mock Music Tool Adapter

默认实现。生成静默 MP3 + 元数据,完全本地,无外部依赖。

生产替换:实现 MusicToolAdapter 协议即可(比如 MatrixMusicAdapter / YuE2Adapter)。
"""
import hashlib
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .tool_adapter import (
    FullSongRequest,
    FullSongResult,
    MusicGenerationRequest,
    MusicResult,
    MusicToolAdapter,
    ensure_dir,
)


def _sha256_file(path: str) -> str:
    if not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _generate_silent_mp3(
    path: str,
    seconds: int,
    sample_rate: int = 22050,
    freq: int = 0,
) -> int:
    """用 ffmpeg 生成 MP3。

    freq == 0  → 纯静音(anullsrc)
    freq >  0  → 单音正弦(用于让不同候选产生**可区分**的字节)

    2026-09-30 修(E2-03 暴露的"3 个候选字节完全相同"):
      之前 3 路都调 anullsrc,输出字节一致 → sha256 相同
      → 无法证明 tool 真的对参数敏感。现在按候选槽位给不同基频。
    """
    ensure_dir(os.path.dirname(path))
    if not shutil.which("ffmpeg"):
        Path(path).write_bytes(b"")
        return 0

    if freq > 0:
        src = f"sine=frequency={freq}:sample_rate={sample_rate}"
    else:
        src = f"anullsrc=channel_layout=mono:sample_rate={sample_rate}"

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", src,
        "-t", str(seconds),
        "-q:a", "7",
        "-acodec", "libmp3lame",
        path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        Path(path).write_bytes(b"\x00")
    return os.path.getsize(path) if os.path.exists(path) else 0


# 各槽位基频(Hz)——让 A/B/C 三个候选在**音频内容**上真正可区分
SLOT_FREQ = {"A": 220, "B": 330, "C": 440}


class MockMusicAdapter:
    """Mock 音乐生成 Adapter

    - generate: 生成 30s 静默 MP3 preview
    - generate_full: 生成 full song MP3
    - 模拟 5s 处理时间(可选)
    - 不消耗外部资源
    """

    name = "MockMusicAdapter"
    is_mock = True

    def __init__(self, simulate_latency_seconds: float = 0.5):
        self.simulate_latency_seconds = simulate_latency_seconds

    def health(self) -> bool:
        return True

    def generate(self, req: MusicGenerationRequest) -> MusicResult:
        ensure_dir(req.output_dir)
        node_id = f"mock_node_{uuid.uuid4().hex[:12]}"
        preview_path = f"{req.output_dir}/{node_id}_preview.mp3"
        # 模拟 latency
        if self.simulate_latency_seconds > 0:
            time.sleep(self.simulate_latency_seconds)

        # 2026-09-30:从 prompt 里识别槽位,给不同基频
        # 让 A/B/C 三个候选在音频内容上真正可区分(而非字节相同)
        slot = None
        for s in ("A", "B", "C"):
            if f"风格 {s}" in (req.prompt or ""):
                slot = s
                break
        freq = SLOT_FREQ.get(slot, 0)

        bytes_written = _generate_silent_mp3(
            preview_path, req.preview_seconds, freq=freq
        )
        sha = _sha256_file(preview_path)

        return MusicResult(
            ok=bytes_written > 0 or os.path.exists(preview_path),
            node_id=node_id,
            preview_path=preview_path,
            preview_url=f"mock://{node_id}_preview.mp3",
            duration_ms=req.preview_seconds * 1000,
            checksum_sha256=sha,
            is_mock=True,
            adapter=self.name,
            artifacts={"bytes": bytes_written, "slot": slot, "freq": freq},
        )

    def generate_full(self, req: FullSongRequest) -> FullSongResult:
        ensure_dir(req.output_dir)
        full_node_id = f"mock_full_{uuid.uuid4().hex[:12]}"
        full_path = f"{req.output_dir}/{full_node_id}.mp3"
        # 完整歌曲 90s(模拟)
        if self.simulate_latency_seconds > 0:
            time.sleep(self.simulate_latency_seconds * 2)
        bytes_written = _generate_silent_mp3(full_path, 90)
        sha = _sha256_file(full_path)

        return FullSongResult(
            ok=bytes_written > 0 or os.path.exists(full_path),
            full_song_path=full_path,
            full_song_url=f"mock://{full_node_id}.mp3",
            full_song_node_id=full_node_id,
            duration_ms=90 * 1000,
            checksum_sha256=sha,
            is_mock=True,
            adapter=self.name,
        )


# ============================================================
#  Music Tool Adapter 组合包装(生成 3 个候选)
# ============================================================

@dataclass
class GenerateCandidatesRequest:
    base_request: MusicGenerationRequest
    slots: List[str] = None           # 默认 ["A", "B", "C"]
    prompt_suffix: Dict[str, str] = None  # slot -> 后缀

    def __post_init__(self):
        if self.slots is None:
            self.slots = ["A", "B", "C"]
        if self.prompt_suffix is None:
            self.prompt_suffix = {
                "A": " 风格 A · 贴耳",
                "B": " 风格 B · 自然",
                "C": " 风格 C · 辽阔",
            }


@dataclass
class CandidateResult:
    slot: str
    status: str                       # "READY" / "FAILED" / "PENDING"
    music_result: Optional[MusicResult] = None
    error: Optional[str] = None


@dataclass
class GenerateCandidatesResult:
    candidates: List[CandidateResult] = field(default_factory=list)
    overall_status: str = "PENDING"   # "READY" / "READY_WITH_FAILURES" / "FAILED"

    def __post_init__(self):
        if not self.candidates:
            return
        ready = sum(1 for c in self.candidates if c.status == "READY")
        failed = sum(1 for c in self.candidates if c.status == "FAILED")
        if failed == 0:
            self.overall_status = "READY"
        elif ready == 0:
            self.overall_status = "FAILED"
        else:
            self.overall_status = "READY_WITH_FAILURES"


from dataclasses import dataclass, field


class MusicToolAdapter:
    """Adapter 包装:把单个生成封装为 3 个候选生成"""

    def __init__(self, inner: MusicToolAdapter):
        self.inner = inner
        self.name = inner.name
        self.is_mock = inner.is_mock

    def health(self) -> bool:
        return self.inner.health()

    def generate_candidates(self, req: GenerateCandidatesRequest) -> GenerateCandidatesResult:
        candidates: List[CandidateResult] = []
        for slot in req.slots:
            slot_req = MusicGenerationRequest(
                prompt=req.base_request.prompt + req.prompt_suffix.get(slot, ""),
                lyrics=req.base_request.lyrics,
                output_dir=req.base_request.output_dir,
                preview_seconds=req.base_request.preview_seconds,
                language=req.base_request.language,
                bpm=req.base_request.bpm,
                key=req.base_request.key,
                genre=req.base_request.genre,
                instruments=req.base_request.instruments,
            )
            try:
                mr = self.inner.generate(slot_req)
                candidates.append(CandidateResult(
                    slot=slot,
                    status="READY" if mr.ok else "FAILED",
                    music_result=mr,
                    error=None if mr.ok else mr.error,
                ))
            except Exception as e:
                candidates.append(CandidateResult(
                    slot=slot,
                    status="FAILED",
                    music_result=None,
                    error=f"{type(e).__name__}: {e}",
                ))
        return GenerateCandidatesResult(candidates=candidates)


# ============================================================
#  Default factory
# ============================================================

def make_default_music_tool() -> MusicToolAdapter:
    """默认 mock 实现"""
    return MockMusicAdapter(simulate_latency_seconds=0.5)
