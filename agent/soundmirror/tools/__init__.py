"""Tools package — 外部服务适配层"""
from .contracts import (
    LarkCredentials,
    SentCard,
    MusicResult,
    AssetUploadResult,
    LarkSender,
    MusicAdapter,
    AssetUploader,
    AudioClipper,
    load_lark_credentials_from_env,
)
from .lark_sender import HttpLarkSender, MockLarkSender, LarkAPIError
from .matrix_adapter import MatrixMusicAdapter, MockMusicAdapter, MatrixConnectionError
from .audio_clipper import FfmpegAudioClipper, MockAudioClipper
from .asset_uploader import MatrixAssetUploader, MockAssetUploader
from .music_pipeline import MusicPipeline, MockMusicPipeline

__all__ = [
    "LarkCredentials", "SentCard", "MusicResult", "AssetUploadResult",
    "LarkSender", "MusicAdapter", "AssetUploader", "AudioClipper",
    "load_lark_credentials_from_env",
    "HttpLarkSender", "MockLarkSender", "LarkAPIError",
    "MatrixMusicAdapter", "MockMusicAdapter", "MatrixConnectionError",
    "FfmpegAudioClipper", "MockAudioClipper",
    "MatrixAssetUploader", "MockAssetUploader",
    "MusicPipeline", "MockMusicPipeline",
]
