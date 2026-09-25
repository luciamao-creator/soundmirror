"""
CLI Driver — 不依赖 Lark,只依赖本地 agent 链路

子命令:
  replay <fixture.json>  重放一段用户旅程,产出每一步的卡 JSON + 最终 ledger
  demo                   跑 happy-path 默认 demo
  eval-card <json>       校验一张卡片是否符合 Lark v1 schema
  state <cs_id>          导出某个 session 的当前 ledger + audit
  validate               跑完整 golden cases 验证
  generate-music         真调一次 matrix,看输出

输出格式约定:
  out_dir/
    step_001_<card-type>.json   <- 卡片 envelope
    step_002_<card-type>.json
    ...
    final_state.json            <- 最终 ledger
    audit_log.json              <- 全部 audit 事件
"""
import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

from .storage import StateStore
from .orchestrator import ActionCatalog
from .orchestrator.orchestrator import Orchestrator
from .director import Director
from .stage import StageProcessor
from .gateway import LarkCardV1Renderer
from .gateway.gateway import Gateway, WebhookRequest
from .tools import (
    MockLarkSender,
    MockMusicAdapter,
    MockAudioClipper,
    MockAssetUploader,
)


# =============================================================
#  Core: 跑一段 webhook 序列
# =============================================================

def make_gateway(db_path: Optional[str] = None) -> tuple[Gateway, StateStore]:
    """组装一个 Gateway(用 mock 工具,不连外部)"""
    if not db_path:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = tmp.name
        tmp.close()

    store = StateStore(db_path)
    catalog = ActionCatalog(str(Path(__file__).parent / "action_catalog.json"))
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
    )
    return gw, store


def run_step(gateway: Gateway, req: WebhookRequest) -> dict:
    """跑一次 webhook,返回结构化结果"""
    resp = gateway.handle_webhook(req)
    return {
        "status": resp.status,
        "error": resp.error,
        "card": resp.response_card,
        "text_reply": resp.text_reply,
    }


# =============================================================
#  Subcommand: replay
# =============================================================

def cmd_replay(args):
    """重放用户旅程 fixture

    fixture 格式: [
      {"event_id": "evt_001", "user_text": "hi"},
      {"event_id": "evt_002", "user_text": "我喜欢..."},
      {"event_id": "evt_003", "action_id": "archaeology.confirm",
       "action_payload": {}},
      ...
    ]
    """
    gateway, store = make_gateway(args.user_id, args.chat_id, db_path=args.db)

    with open(args.fixture) as f:
        fixture_data = json.load(f)

    out_dir = Path(args.out_dir) if args.out_dir else Path("./out") / Path(args.fixture).stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # 兼容两种格式:
    #   1. 平铺数组: [ {step1}, {step2}, ... ]
    #   2. 包装格式: { "user_id": "...", "chat_id": "...", "steps": [...] }
    if isinstance(fixture_data, list):
        steps = fixture_data
        user_id = args.user_id
        chat_id = args.chat_id
    else:
        steps = fixture_data.get("steps", [])
        user_id = fixture_data.get("user_id", args.user_id)
        chat_id = fixture_data.get("chat_id", args.chat_id)

    # 追踪最近一次的 pending_action_id(用于 confirm.accept 自动带 id)
    last_pending_id: Optional[str] = None

    print(f"=== Replay {len(steps)} steps → {out_dir} ===\n")

    for i, step in enumerate(steps, 1):
        event_id = step.get("event_id") or f"evt_replay_{i:04d}"
        if "user_text" in step:
            req = WebhookRequest(
                event_id=event_id,
                user_id=user_id,
                chat_id=chat_id,
                message_id=f"om_{event_id}",
                text=step["user_text"],
            )
        elif "action_id" in step:
            action_id = step["action_id"]
            payload = dict(step.get("action_payload", {}))

            # 自动: confirm.accept 带上次响应的 pending_action_id
            if action_id in ("confirm.accept", "confirm.cancel") and "pending_action_id" not in payload:
                if last_pending_id:
                    payload["pending_action_id"] = last_pending_id

            req = WebhookRequest(
                event_id=event_id,
                user_id=user_id,
                chat_id=chat_id,
                message_id=f"om_{event_id}",
                text="",
                action_id=action_id,
                action_payload=payload,
            )
        elif "admin_state_mutation" in step:
            # 仅 CLI driver 用: bypass stage flow 直接 mutate
            sessions = gateway._list_user_sessions(user_id)
            if sessions:
                cs_id = sessions[0]["creative_session_id"]
                try:
                    gateway.store.apply_mutation(cs_id, step["admin_state_mutation"], user_id)
                    print(f"   [admin mutation applied to {cs_id[:12]}...]")
                except Exception as e:
                    print(f"!! admin_state_mutation failed: {e}")
            continue
        else:
            print(f"!! Step {i}: bad step format: {step}")
            continue

        result = run_step(gateway, req)

        # 输出卡到文件
        if result["card"]:
            # 自动从 confirm 卡抓 pending_action_id, 用于下一步自动 confirm
            for elem in result["card"].get("elements", []):
                if elem.get("tag") == "action":
                    for btn in elem.get("actions", []):
                        pid = btn.get("value", {}).get("pending_action_id")
                        if pid:
                            last_pending_id = pid
                            break
                    if last_pending_id:
                        break

            card_type = result["card"].get("header", {}).get("title", {}).get("content", "card")
            safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in card_type[:30])
            card_path = out_dir / f"step_{i:03d}_{safe}.json"
            card_path.write_text(json.dumps(result["card"], ensure_ascii=False, indent=2), encoding="utf-8")

        # 也记录原始 envelope
        env_path = out_dir / f"step_{i:03d}_envelope.json"
        env_path.write_text(json.dumps({
            "event_id": event_id,
            "input": step,
            "result": {k: v for k, v in result.items() if k != "card"},
            "card_card_type": (result["card"] or {}).get("card_type"),
            "card_title": (result["card"] or {}).get("header", {}).get("title", {}).get("content"),
            "last_pending_id": last_pending_id,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        title = (result["card"] or {}).get("header", {}).get("title", {}).get("content", "?")
        print(f"[Step {i}] status={result['status']:>10s} card={title}")

    # 最终 ledger + audit
    sessions = gateway._list_user_sessions(user_id)
    if sessions:
        cs_id = sessions[0]["creative_session_id"]
        full_session = store.get_session(cs_id)
        (out_dir / "final_state.json").write_text(
            json.dumps({
                "creative_session_id": cs_id,
                "state_revision": full_session["state_revision"],
                "ledger": full_session["ledger"],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # audit
        audit = store.read_audit(cs_id, limit=500)
        (out_dir / "audit_log.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n=== Session {cs_id[:20]}... ===")
        print(f"  state_revision: {full_session['state_revision']}")
        print(f"  current_stage: {full_session['ledger'].get('current_stage')}")
        print(f"  audit events: {len(audit)}")

    print(f"\n✓ All outputs in {out_dir}")


# =============================================================
#  Subcommand: demo
# =============================================================

DEMO_STEPS = [
    {"event_id": "demo_001", "user_text": "hi"},
    {"event_id": "demo_002", "user_text": "我喜欢公路片那种孤独,尤其是开车过沙漠的镜头"},
    {"event_id": "demo_003", "user_text": "喜欢日本电影里安静的那种节奏"},
    {"event_id": "demo_004", "action_id": "archaeology.confirm"},
]


def cmd_demo(args):
    """跑 happy-path 默认 demo"""
    fixture_path = Path(args.out_dir) / "_fixture.json"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(json.dumps(DEMO_STEPS, ensure_ascii=False, indent=2), encoding="utf-8")
    args.fixture = str(fixture_path)
    cmd_replay(args)


# =============================================================
#  Subcommand: eval-card
# =============================================================

def cmd_eval_card(args):
    """校验卡片是否 Lark v1 合规"""
    with open(args.card_file) as f:
        card = json.load(f)
    renderer = LarkCardV1Renderer()
    issues = []

    # 必填字段
    for required in ("config", "header", "elements"):
        if required not in card:
            issues.append(f"missing required field: {required}")

    # header 字段
    header = card.get("header", {})
    if "title" not in header or "tag" not in header.get("title", {}):
        issues.append("header.title.tag missing")
    if "template" in header and header["template"] not in (
        "blue", "green", "red", "yellow", "indigo", "purple", "grey", "orange"
    ):
        issues.append(f"header.template invalid: {header['template']}")

    # 元素里每 button 必有 action_id
    for i, elem in enumerate(card.get("elements", [])):
        if elem.get("tag") == "action":
            for j, btn in enumerate(elem.get("actions", [])):
                if "value" not in btn or "action_id" not in btn.get("value", {}):
                    issues.append(f"element[{i}].action[{j}] missing action_id in value")
                if btn.get("type") not in ("primary", "danger", "default"):
                    issues.append(f"element[{i}].action[{j}] type invalid: {btn.get('type')}")

    if issues:
        print(f"❌ Card has {len(issues)} issue(s):")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)
    print(f"✓ Card valid Lark v1 ({len(card.get('elements', []))} elements)")


# =============================================================
#  Subcommand: state
# =============================================================

def cmd_state(args):
    """导出某个 session 当前 ledger + audit"""
    if args.db and Path(args.db).exists():
        store = StateStore(args.db)
        session = store.get_session(args.cs_id)
        if not session:
            print(f"Session {args.cs_id} not found in {args.db}")
            sys.exit(1)
        audit = store.read_audit(args.cs_id)
        out = {
            "session": {
                "creative_session_id": session["creative_session_id"],
                "state_revision": session["state_revision"],
                "archived": session["archived"],
                "ledger": session["ledger"],
            },
            "audit_count": len(audit),
            "audit_sample": audit[:10],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"No DB found at {args.db}")


# =============================================================
#  Subcommand: validate
# =============================================================

def cmd_validate(args):
    """跑 golden cases 验证"""
    import subprocess
    cmd = [
        sys.executable, "-m", "pytest",
        str(Path(__file__).parent / "tests" / "integration" / "test_golden_cases.py"),
        "-v", "--tb=short",
    ]
    result = subprocess.run(cmd, cwd=str(Path(__file__).parent))
    sys.exit(result.returncode)


# =============================================================
#  Subcommand: generate-music
# =============================================================

def cmd_generate_music(args):
    """真调一次 matrix,看输出"""
    from .tools import MatrixMusicAdapter, MockMusicAdapter, MatrixConnectionError
    adapter = MatrixMusicAdapter()
    try:
        adapter.validate_schema()
    except MatrixConnectionError as e:
        print(f"⚠️ mcode-tools not available: {e}")
        print("Falling back to mock...")
        adapter = MockMusicAdapter()

    prompt = args.prompt or "A peaceful acoustic folk ballad with warm guitar and soft female vocal"
    lyrics = args.lyrics or "[verse]\nWalking down the dusty road\nThe sun is fading"
    result = adapter.generate(prompt=prompt, lyrics=lyrics, bitrate=128000)

    out = {
        "success": result.success,
        "node_id": result.node_id,
        "file_name": result.file_name,
        "audio_url": result.audio_url,
        "model": result.model,
        "error": result.error,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

    if result.success and result.audio_url:
        print(f"\n✓ Audio URL: {result.audio_url}")
        print(f"  (Lark 卡片用 ↑ audio_url,30s 剪裁后变成 preview_url)")


def cmd_generate_song(args):
    """完整链路:Matrix 生成 → ffmpeg 30s 剪裁 → 1h 上传"""
    from .tools import MusicPipeline, MockMusicPipeline

    use_mock = args.mock
    if use_mock:
        pipeline = MockMusicPipeline()
        print("🔧 Using mock pipeline")
    else:
        pipeline = MusicPipeline(prefer_real=True)
        print("🎵 Using real pipeline (auto fallback if mcode/ffmpeg 不可用)")

    prompt = args.prompt or (
        "A peaceful acoustic folk ballad with warm acoustic guitar "
        "and gentle female vocals, melancholic but hopeful"
    )
    lyrics = args.lyrics or (
        "[verse]\nWalking down the dusty road\nThe sun is fading slow\n"
        "[chorus]\nI will find my way back home\nWhere the morning light has shown"
    )

    print(f"\nPrompt: {prompt[:80]}...")
    print(f"Lyrics:\n{lyrics[:100]}...")
    print()

    out_dir = args.out_dir
    print(f"⏳ Generating + clipping + uploading...")
    result = pipeline.generate_with_preview(
        prompt=prompt, lyrics=lyrics,
        output_dir=out_dir, preview_seconds=args.duration,
    )

    if not result["music"].success:
        print(f"\n❌ Generation failed: {result['music'].error}")
        return 1

    print(f"\n✓ Matrix generated:")
    print(f"  node_id:    {result['music'].node_id}")
    print(f"  file_name:  {result['music'].file_name}")
    print(f"  audio_url:  {result['music'].audio_url}")
    print(f"  model:      {result['music'].model}")

    if result.get("preview_path"):
        size = Path(result["preview_path"]).stat().st_size if Path(result["preview_path"]).exists() else 0
        print(f"\n✓ 30s Preview:")
        print(f"  local:  {result['preview_path']}")
        print(f"  size:   {size} bytes")

    if result.get("preview_url"):
        print(f"\n✓ Shareable URL (1-hour):")
        print(f"  {result['preview_url']}")

    if result.get("error"):
        print(f"\n⚠️ Pipeline warning: {result['error']}")

    # Lark 卡
    card = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🎵 整首完成"},
            "template": "purple",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**node_id**: `{result['music'].node_id}`"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**预览**: [30s mp3]({result.get('preview_url', '#')})"}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": f"原曲:{result['music'].audio_url or 'N/A'}"}]},
            {"tag": "hr"},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "✓ 满意"}, "type": "primary",
                 "value": {"action_id": "review.approve", "node_id": result["music"].node_id}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "✏️ 修改"}, "type": "danger",
                 "value": {"action_id": "revision.request", "feedback": "再调整"}},
            ]},
        ],
    }
    card_path = Path(out_dir) / f"audio_card_{result['music'].node_id[:12]}.json"
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📝 Lark card saved: {card_path}")

    summary_path = Path(out_dir) / f"summary_{result['music'].node_id[:12]}.json"
    summary = {
        "music": {
            "node_id": result["music"].node_id,
            "file_name": result["music"].file_name,
            "audio_url": result["music"].audio_url,
            "model": result["music"].model,
        },
        "preview": {
            "path": result.get("preview_path"),
            "url": result.get("preview_url"),
            "uploaded": result.get("uploaded", False),
        },
        "card_json": str(card_path),
        "ts": time.time(),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📝 Summary saved: {summary_path}")

    return 0


def cmd_send_to_lark(args):
    """真发卡到飞书(需要 LARK_APP_ID + LARK_APP_SECRET 环境变量)

    不发卡(只有生成)的话,加 --dry-run
    """
    from .tools import load_lark_credentials_from_env, HttpLarkSender, MockLarkSender, LarkAPIError

    creds = load_lark_credentials_from_env()

    if args.dry_run:
        sender = MockLarkSender()
        print("🔧 DRY-RUN: Using MockLarkSender (not actually sending)")
    else:
        if not creds.app_id or not creds.app_secret:
            print("❌ Missing LARK_APP_ID / LARK_APP_SECRET env vars")
            print("  Set them in .env or env vars, then re-run")
            print("  Or pass --dry-run to test without real Lark API call")
            return 1
        sender = HttpLarkSender(creds)
        print(f"🌐 Using HttpLarkSender with app_id={creds.app_id[:10]}...")

    # 读卡
    card_path = Path(args.card_file)
    if not card_path.exists():
        print(f"❌ Card file not found: {card_path}")
        return 1
    card = json.loads(card_path.read_text(encoding="utf-8"))

    # 发卡
    try:
        result = sender.send_card(
            chat_id=args.chat_id,
            card_json=card,
        )
        print(f"\n✓ Card sent:")
        print(f"  chat_id:    {args.chat_id}")
        print(f"  message_id: {result.message_id}")
        print(f"  sent_at:    {result.sent_at}")
        return 0
    except LarkAPIError as e:
        print(f"\n❌ Send failed: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ Unexpected error: {type(e).__name__}: {e}")
        return 1


def cmd_serve_webhook(args):
    """起 HTTP server 接收 Lark webhook

    Dry-run 模式用 MockLarkSender(发到本地内存),可以测试整个 webhook → gateway → 卡回传链路。

    真实模式需要 cloudflared/ngrok 暴露 URL 到公网,飞书才能访问。
    """
    from .gateway import start_server, LarkWebhookHandler
    from .tools import MockLarkSender, HttpLarkSender, load_lark_credentials_from_env

    # 装好 gateway
    sender = MockLarkSender() if args.dry_run else None
    if sender is None:
        creds = load_lark_credentials_from_env()
        if not creds.app_id or not creds.app_secret:
            print("❌ Missing LARK_APP_ID / LARK_APP_SECRET env vars")
            print("  Or pass --dry-run to use MockLarkSender")
            return 1
        sender = HttpLarkSender(creds)
        print(f"🌐 Using HttpLarkSender with app_id={creds.app_id[:10]}...")

    gw, store = make_gateway(args.user_id, args.chat_id)
    # 把 sender 注入 handler,这样 _dispatch 可以调
    LarkWebhookHandler.sender = sender

    print(f"\n🎯 Server config:")
    print(f"  Host: {args.host}")
    print(f"  Port: {args.port}")
    print(f"  Sender: {'Mock' if args.dry_run else 'Http'}")
    print(f"\n📡 Endpoints:")
    print(f"  POST /webhook/url_verify — Lark URL 验证")
    print(f"  POST /webhook — Lark 事件入口")
    print(f"  GET  /health — 健康检查")

    if not args.dry_run:
        print(f"\n⚠️ 真实模式:记得用 cloudflared/ngrok 把这个 server 暴露到公网")
        print(f"  cloudflared tunnel --url http://localhost:{args.port}")
        print(f"  然后把 trycloudflare URL 填到 Lark 后台 webhook URL")

    print(f"\n✓ Starting server...")
    try:
        start_server(
            host=args.host, port=args.port, gateway=gw, sender=sender,
        )
    except KeyboardInterrupt:
        print("\n🛑 Server stopped")
    return 0


def make_gateway(user_id: str = "u_demo", chat_id: str = "oc_demo",
                 db_path: Optional[str] = None) -> tuple:
    """组装一个 Gateway(M1 demo 用)"""
    import tempfile
    if not db_path:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = tmp.name

    store = StateStore(db_path)
    catalog = ActionCatalog(str(Path(__file__).parent / "action_catalog.json"))
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
    )
    return gw, store


# =============================================================
#  Main
# =============================================================

def main():
    parser = argparse.ArgumentParser(
        description="SoundMirror CLI - 跑 agent 内容输出,不依赖飞书 URL"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # replay
    p_replay = sub.add_parser("replay", help="重放用户旅程 JSON")
    p_replay.add_argument("fixture", help="fixture JSON 路径")
    p_replay.add_argument("--out-dir", default=None)
    p_replay.add_argument("--db", default=None)
    p_replay.add_argument("--user-id", default="u_demo")
    p_replay.add_argument("--chat-id", default="oc_demo")
    p_replay.set_defaults(func=cmd_replay)

    # demo
    p_demo = sub.add_parser("demo", help="跑默认 happy-path demo")
    p_demo.add_argument("--out-dir", default="./out/demo")
    p_demo.add_argument("--db", default=None)
    p_demo.add_argument("--user-id", default="u_demo")
    p_demo.add_argument("--chat-id", default="oc_demo")
    p_demo.set_defaults(func=cmd_demo)

    # eval-card
    p_eval = sub.add_parser("eval-card", help="校验一张 Lark 卡 schema")
    p_eval.add_argument("card_file", help="卡片 JSON 文件")
    p_eval.set_defaults(func=cmd_eval_card)

    # state
    p_state = sub.add_parser("state", help="读 session 当前状态")
    p_state.add_argument("cs_id", help="creative_session_id")
    p_state.add_argument("--db", default=None)
    p_state.set_defaults(func=cmd_state)

    # validate
    p_val = sub.add_parser("validate", help="跑全部 golden case 验证")
    p_val.set_defaults(func=cmd_validate)

    # generate-music
    p_music = sub.add_parser("generate-music", help="真调 matrix 生成一首")
    p_music.add_argument("--prompt", default=None)
    p_music.add_argument("--lyrics", default=None)
    p_music.set_defaults(func=cmd_generate_music)

    # generate-song — 完整 pipeline
    p_song = sub.add_parser("generate-song", help="完整链路:matrix + 30s clip + upload")
    p_song.add_argument("--prompt", default=None)
    p_song.add_argument("--lyrics", default=None)
    p_song.add_argument("--out-dir", default="./out/audio")
    p_song.add_argument("--duration", type=int, default=30, help="preview duration seconds")
    p_song.add_argument("--mock", action="store_true", help="use mock pipeline")
    p_song.set_defaults(func=cmd_generate_song)

    # send-to-lark — 实际发卡到飞书
    p_lark = sub.add_parser("send-to-lark", help="发 Lark 卡到飞书 chat(需要 creds)")
    p_lark.add_argument("card_file", help="Lark card JSON 文件")
    p_lark.add_argument("--chat-id", required=True, help="飞书 chat_id (oc_xxx)")
    p_lark.add_argument("--dry-run", action="store_true", help="不发实际,只 mock")
    p_lark.set_defaults(func=cmd_send_to_lark)

    # serve-webhook — 起 HTTP server 收 Lark webhook
    p_serve = sub.add_parser("serve-webhook", help="起 HTTP server 接 Lark webhook")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--dry-run", action="store_true",
                          help="用 mock sender(不发真卡到飞书)")
    p_serve.add_argument("--user-id", default="u_demo")
    p_serve.add_argument("--chat-id", default="oc_demo")
    p_serve.set_defaults(func=cmd_serve_webhook)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
