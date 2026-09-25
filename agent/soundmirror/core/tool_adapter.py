"""Tool Adapter 接口 — 让 Agent Core 与具体后端解耦

本阶段只提供 mock 实现。后续切换 Matrix/MiniMax Music/YuE2 只需替换 adapter。
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
import time


@dataclass
class MusicGenerationRequest:
    prompt: str
    lyrics: str
    output_dir: str
    preview_seconds: int = 30
    language: str = "zh"             # 强制语言对齐(中文/英文)
    bpm: Optional[int] = None
    key: Optional[str] = None
    genre: Optional[str] = None
    instruments: Optional[List[str]] = None


@dataclass
class MusicResult:
    ok: bool
    node_id: Optional[str] = None        # 音乐模型给的节点 ID
    preview_path: Optional[str] = None
    preview_url: Optional[str] = None
    duration_ms: int = 0
    checksum_sha256: Optional[str] = None
    is_mock: bool = False
    adapter: str = "unknown"
    error: Optional[str] = None
    artifacts: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FullSongRequest:
    node_id: str
    is_mock: bool
    output_dir: str


@dataclass
class FullSongResult:
    ok: bool
    full_song_path: Optional[str] = None
    full_song_url: Optional[str] = None
    full_song_node_id: Optional[str] = None
    duration_ms: int = 0
    checksum_sha256: Optional[str] = None
    is_mock: bool = False
    adapter: str = "unknown"
    error: Optional[str] = None


@runtime_checkable
class MusicToolAdapter(Protocol):
    """音乐生成 Adapter 接口

    实现可替换:Mock / Matrix / MiniMax Music / YuE2 / 任何其他
    """

    name: str
    is_mock: bool

    def health(self) -> bool:
        """检查后端是否可用"""
        ...

    def generate(self, req: MusicGenerationRequest) -> MusicResult:
        """生成 30s 预览 + (可选)上传"""
        ...

    def generate_full(self, req: FullSongRequest) -> FullSongResult:
        """基于节点生成完整歌曲"""
        ...


# ============================================================
#  Lark Tool Adapter(本阶段 mock)
# ============================================================

@dataclass
class SendCardRequest:
    chat_id: str
    title: str
    content: str
    actions: List[Dict[str, Any]] = field(default_factory=list)
    template: str = "blue"
    update_message_id: Optional[str] = None      # 用 update 替代 send


@dataclass
class SendCardResult:
    ok: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


@runtime_checkable
class LarkToolAdapter(Protocol):
    """飞书 Adapter 接口(本阶段 mock)"""

    name: str
    is_mock: bool

    def send_card(self, req: SendCardRequest) -> SendCardResult:
        ...

    def reply_text(self, chat_id: str, text: str) -> SendCardResult:
        ...


# ============================================================
#  Util: time a tool call
# ============================================================

@dataclass
class TimedResult:
    result: Any
    duration_ms: float
    error: Optional[str] = None


def time_call(fn, *args, **kwargs) -> TimedResult:
    """执行函数,返回结果 + 耗时 + 错误"""
    start = time.time()
    error = None
    result = None
    try:
        result = fn(*args, **kwargs)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    return TimedResult(
        result=result,
        duration_ms=(time.time() - start) * 1000,
        error=error,
    )


def ensure_dir(p: str):
    Path(p).mkdir(parents=True, exist_ok=True)
