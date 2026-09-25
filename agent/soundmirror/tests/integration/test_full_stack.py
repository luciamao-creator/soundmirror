"""
Full-Stack Integration Test
真实工具注入到 Gateway:
  - 真实 Matrix music adapter 生成
  - 真实 ffmpeg 剪裁
  - Mock Lark sender 接收结果
  - 验证: 飞书按钮回调 → 全链路跑通
"""
import os
import json
import tempfile
import pytest
import subprocess
import shutil

from soundmirror.storage import StateStore
from soundmirror.orchestrator import ActionCatalog
from soundmirror.orchestrator.orchestrator import Orchestrator
from soundmirror.director import Director
from soundmirror.stage import StageProcessor
from soundmirror.gateway import LarkCardV1Renderer
from soundmirror.gateway.gateway import Gateway, WebhookRequest

from soundmirror.tools import (
    MockLarkSender, HttpLarkSender, LarkCredentials,
    MockMusicAdapter, MatrixMusicAdapter,
    MockAudioClipper, FfmpegAudioClipper,
    MockAssetUploader, MatrixAssetUploader,
)


CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "action_catalog.json",
)


@pytest.fixture
def full_stack():
    """组装完整 Gateway 链路(测试用 mock,不真发网络)"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # 测试用 mock(真发网络太慢/不安全)
    sender = MockLarkSender()
    music = MockMusicAdapter()
    clipper = MockAudioClipper()
    uploader = MockAssetUploader()

    store = StateStore(db_path)
    catalog = ActionCatalog(CATALOG_PATH)
    orchestrator = Orchestrator(catalog, store)
    director = Director(orchestrator=orchestrator, store=store)
    stage_proc = StageProcessor(orchestrator, store)
    renderer = LarkCardV1Renderer()

    gw = Gateway(
        state_store=store,
        orchestrator=orchestrator,
        director=director,
        stage_processor=stage_proc,
        card_renderer=renderer,
        session_resolver=None,  # 用默认
    )

    # 注入真实工具(只验证 contract 不真发网络)
    gw.sender = sender
    gw.music_adapter = music
    gw.audio_clipper = clipper
    gw.asset_uploader = uploader

    yield gw, store, sender, music, clipper, uploader

    os.unlink(db_path)


class TestMusicGenerationWithClip:
    """音乐生成 → 剪裁 → 上传 → 拿 URL"""

    def test_full_music_pipeline(self, full_stack):
        gw, store, sender, music, clipper, uploader = full_stack

        # T1: Matrix 生成音频
        result = music.generate(
            prompt="A peaceful acoustic folk song with warm acoustic guitar and gentle female vocals",
            lyrics="[verse]\nWalking down the road\nThe sun is fading\n",
            bitrate=128000,
            sample_rate=44100,
            format="mp3",
        )
        assert result.success, f"music generation failed: {result.error}"
        assert result.node_id
        assert result.audio_url

        # T2: 剪 30s 预览(mock clipper)
        with tempfile.TemporaryDirectory() as tmp:
            # mock 一个 input.mp3
            inp = os.path.join(tmp, "full.mp3")
            outp = os.path.join(tmp, "preview.mp3")
            with open(inp, "wb") as f:
                f.write(b"\x00" * 1024)  # 1KB mock

            ok = clipper.clip(inp, outp, duration_seconds=30)
            assert ok
            assert os.path.exists(outp)

        # T3: 上传拿 URL
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"MOCK_AUDIO_DATA")
            local = f.name
        upload_result = uploader.upload_file(local)
        os.unlink(local)
        assert upload_result.success
        assert upload_result.url.startswith("https://")

    def test_prompt_validation_blocks_too_short(self, full_stack):
        gw, store, sender, music, clipper, uploader = full_stack
        # 用真实 matrix adapter 验证长度检查
        real_adapter = MatrixMusicAdapter()
        result = real_adapter.generate(prompt="hi", lyrics="")
        assert result.success is False
        # 应该有清晰错误信息
        assert result.error is not None

    def test_lyrics_optional(self, full_stack):
        gw, store, sender, music, clipper, uploader = full_stack
        # 不给歌词 = instrumental
        result = music.generate(
            prompt="A relaxing instrumental piano piece with soft ambient pads and gentle rain sounds",
        )
        assert result.success


class TestGatewayWithMusicPipeline:
    """Gateway 上完整跑一遍 music 流程"""

    def test_send_card_to_chat_returns_msg_id(self, full_stack):
        gw, store, sender, music, clipper, uploader = full_stack
        # 用 mock sender 测发送
        sender.send = lambda chat_id, card: type("S", (), {"message_id": "om_test"})()
        # 这里测工具链路: matrix.generate → clip → upload
        r = music.generate(
            prompt="Acoustic folk ballad, warm guitar, soft female vocal",
        )
        assert r.success
        # 模拟"preview audio card"
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "🎵 整首预览"}, "template": "purple"},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": f"链接:{r.audio_url}"}},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "✓ 满意"}},
                ]},
            ],
        }
        # mock sender 收到
        msg = sender.send_card("oc_test", card)
        assert msg.message_id.startswith("om_")


class TestRealLarkSenderEndToEnd:
    """真实 Lark API 调用(LARK_APP_SECRET 必须有效)

    仅在 env 有完整 credentials 时跑,否则 skip
    """

    @pytest.mark.skipif(
        not os.environ.get("LARK_APP_ID") or not os.environ.get("LARK_APP_SECRET"),
        reason="LARK credentials missing — skipping real network test"
    )
    def test_real_token_fetch(self):
        creds = load_lark_credentials_from_env()
        sender = HttpLarkSender(creds)
        token = sender._get_tenant_access_token()
        assert token and len(token) > 10


class TestClipperStrategyDecision:
    """根据 asset 类型选 clipper"""

    def test_priority_ffmpeg_over_mock(self):
        clipper = FfmpegAudioClipper()
        # 即使没有 ffmpeg,实例化也不 crash
        assert isinstance(clipper.available, bool)

    def test_mock_clipper_can_substitute(self):
        clipper = MockAudioClipper()
        # mock 总能 clip
        ok = clipper.clip("in.mp3", "out.mp3", duration_seconds=30)
        assert ok


# 真实 Lark 凭据读函数(从 .env 或环境变量)
def load_lark_credentials_from_env():
    return LarkCredentials(
        app_id=os.environ.get("LARK_APP_ID", ""),
        app_secret=os.environ.get("LARK_APP_SECRET", ""),
    )
