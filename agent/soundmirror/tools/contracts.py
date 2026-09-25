"""
Tools Contracts — 接口抽象 + DI 注入层

工具层(连接真实外部服务)的契约,让 Gateway / Orchestrator 测试时能注入 mock
"""
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol


@dataclass
class LarkCredentials:
    app_id: str
    app_secret: str
    verification_token: Optional[str] = None
    encrypt_key: Optional[str] = None


@dataclass
class SentCard:
    """API 返回"""
    message_id: str
    chat_id: str
    sent_at: float = field(default_factory=time.time)


@dataclass
class MusicResult:
    """音乐生成结果"""
    success: bool
    node_id: Optional[str] = None
    file_name: Optional[str] = None
    audio_url: Optional[str] = None        # 短期签名 URL
    duration_seconds: Optional[int] = None
    model: str = "unknown"                  # M0C 验证 matrix 不返回 model
    raw_response: Optional[dict] = None
    error: Optional[str] = None


@dataclass
class AssetUploadResult:
    """上传结果"""
    success: bool
    url: Optional[str] = None       # 1-hour HTTPS URL
    node_id: Optional[str] = None
    error: Optional[str] = None
    url_expires_in: Optional[int] = None   # URL 剩余有效秒数(PM P1-3)
    object_key: Optional[str] = None       # 稳定存储 key(用于 refresh)


class LarkSender(Protocol):
    """Lark 消息发送抽象"""

    def send_card(self, chat_id: str, card_json: dict, reply_to: Optional[str] = None) -> SentCard: ...
    def update_card(self, message_id: str, card_json: dict) -> bool: ...
    def reply_text(self, chat_id: str, text: str, at_user: Optional[str] = None) -> SentCard: ...


class MusicAdapter(Protocol):
    """音乐生成抽象"""

    def generate(self, prompt: str, lyrics: str, **kwargs) -> MusicResult: ...
    def get_audio_url(self, node_id: str) -> Optional[str]: ...


class AssetUploader(Protocol):
    """音频/文件上传抽象"""

    def upload_file(self, local_path: str) -> AssetUploadResult: ...
    def upload_bytes(self, data: bytes, file_name: str) -> AssetUploadResult: ...


class AudioClipper(Protocol):
    """音频剪裁抽象"""

    def clip(self, input_path: str, output_path: str, duration_seconds: int = 30,
            start_seconds: int = 0) -> bool: ...
    def clip_from_url(self, audio_url: str, output_path: str,
                     duration_seconds: int = 30) -> bool: ...


def load_lark_credentials_from_env() -> LarkCredentials:
    """从 .env 或环境变量加载(不在 chat 内回显 secret)"""
    app_id = os.environ.get("LARK_APP_ID") or os.environ.get("LARK_APPID")
    app_secret = os.environ.get("LARK_APP_SECRET") or os.environ.get("LARK_SECRET")
    if not app_id or not app_secret:
        # 从 .env 文件读
        env_path = Path(__file__).parent.parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("LARK_APP_ID="):
                    app_id = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("LARK_APP_SECRET="):
                    app_secret = line.split("=", 1)[1].strip().strip('"').strip("'")

    return LarkCredentials(
        app_id=app_id or "",
        app_secret=app_secret or "",
    )
