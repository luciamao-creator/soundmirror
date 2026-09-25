"""
Entry script to start the Lark webhook server.

Assembles a Gateway with real LarkSender + Matrix music pipeline, and binds it
to a ThreadingHTTPServer listening on 0.0.0.0:$PORT.
"""
import os
import sys
from pathlib import Path

# Ensure project root on path (parent of soundmirror package)
HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)

from soundmirror.gateway.webhook_server import start_server
from soundmirror.gateway.gateway import Gateway
from soundmirror.gateway.card_renderer import LarkCardV1Renderer
from soundmirror.storage import StateStore
from soundmirror.orchestrator.orchestrator import Orchestrator
from soundmirror.orchestrator.catalog import ActionCatalog
from soundmirror.director import Director
from soundmirror.stage.handlers import StageProcessor
from soundmirror.tools.lark_sender import HttpLarkSender
from soundmirror.tools.contracts import load_lark_credentials_from_env
from soundmirror.tools.music_pipeline import MusicPipeline
from soundmirror.tools.matrix_adapter import MatrixMusicAdapter
from soundmirror.tools.asset_uploader import MatrixAssetUploader
from soundmirror.tools.audio_clipper import FfmpegAudioClipper


def build_pipeline():
    """Real Matrix pipeline with fallback_allowed (mock if Matrix fails)."""
    music_gen = MatrixMusicAdapter(timeout_seconds=180)
    uploader = MatrixAssetUploader()
    clipper = FfmpegAudioClipper()
    pipeline = MusicPipeline(
        music_gen=music_gen,
        uploader=uploader,
        clipper=clipper,
        prefer_real=True,
        fallback_policy="fallback_allowed",
    )
    return pipeline


def main():
    port = int(os.environ.get("WEBHOOK_PORT", "18080"))
    verification_token = os.environ.get("LARK_VERIFICATION_TOKEN", "")
    encrypt_key = os.environ.get("LARK_ENCRYPT_KEY", "")
    db_path = os.environ.get(
        "SOUNDMIRROR_DB",
        os.path.join(HERE, "out", "lark_webhook.db"),
    )

    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    creds = load_lark_credentials_from_env()
    print(f"📋 LARK_APP_ID loaded: {creds.app_id[:8]}...{creds.app_id[-4:] if len(creds.app_id) > 12 else ''}")
    print(f"📋 LARK_APP_SECRET loaded: {'yes' if creds.app_secret else 'no'} ({len(creds.app_secret) if creds.app_secret else 0} chars)")

    # 2026-09-22 (Bridge verification): 如果 LARK_APP_SECRET 是 REDACTED 占位符
    # (用户尚未轮换) 或 SOUNDMIRROR_USE_MOCK_LARK=1,用 MockLarkSender
    # 避免 LarkSender.send_card 抛 LarkAPIError 拖死 background thread
    if (not creds.app_secret
        or "<REDACTED" in creds.app_id
        or os.environ.get("SOUNDMIRROR_USE_MOCK_LARK") == "1"):
        from soundmirror.tools.lark_sender import MockLarkSender
        sender = MockLarkSender()
        print("🔧 使用 MockLarkSender(无真实 Lark 凭证)")
    else:
        sender = HttpLarkSender(creds)

    store = StateStore(db_path)
    catalog = ActionCatalog(str(Path(HERE) / "action_catalog.json"))

    # 2026-09-22 改(Bridge E2E):从共享模块 build mutation_handlers
    # 之前 webhook 路径没有 handlers → 按钮 action 全 noop,stage 不推进
    from soundmirror.orchestrator.mutation_handlers import build_mutation_handlers
    handlers = build_mutation_handlers(store)
    orchestrator = Orchestrator(catalog, store, mutation_handlers=handlers)
    director = Director(orchestrator=orchestrator, store=store)
    stage_proc = StageProcessor(orchestrator, store)
    renderer = LarkCardV1Renderer()

    gateway = Gateway(
        state_store=store,
        orchestrator=orchestrator,
        director=director,
        stage_processor=stage_proc,
        card_renderer=renderer,
    )

    print(f"📦 DB: {db_path}")
    print(f"🔌 Listening on 0.0.0.0:{port}")

    start_server(
        host="0.0.0.0",
        port=port,
        gateway=gateway,
        sender=sender,
        verification_token=verification_token,
        encrypt_key=encrypt_key,
    )


if __name__ == "__main__":
    main()
