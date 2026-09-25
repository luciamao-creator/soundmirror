"""
真实慢 action 验证:monkeypatch 让 _proposal_confirm 慢 5s,
确认 webhook ACK 仍 < 1s,5s 后通过 update_card 替换"处理中"卡。
"""
import json
import threading
import time
import urllib.request


def test_proposal_confirm_acks_fast_even_when_slow():
    """
    模拟 proposal.confirm 处理耗时 5s(模拟 Matrix 真实场景)。
    webhook ACK 必须在 < 1s 内返回。
    """
    # 不通过 fixture,而是 patch gateway 的 handle_webhook 让它慢
    import sys
    sys.path.insert(0, "/workspace/voice-persona-assessment/agent")

    from soundmirror.storage import StateStore
    from soundmirror.orchestrator import ActionCatalog
    from soundmirror.orchestrator.orchestrator import Orchestrator
    from soundmirror.director import Director
    from soundmirror.stage import StageProcessor
    from soundmirror.gateway import LarkCardV1Renderer, LarkWebhookHandler
    from soundmirror.gateway.gateway import Gateway, WebhookRequest
    from soundmirror.tools import MockLarkSender
    from http.server import ThreadingHTTPServer
    from pathlib import Path
    import tempfile
    import socket

    # 找空闲端口
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    # 起 server
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = StateStore(db)
    catalog = ActionCatalog(str(Path("/workspace/voice-persona-assessment/agent/soundmirror/action_catalog.json")))
    orch = Orchestrator(catalog, store)
    director = Director(orchestrator=orch, store=store)
    stage_proc = StageProcessor(orch, store)
    renderer = LarkCardV1Renderer()
    sender = MockLarkSender()

    gw = Gateway(
        state_store=store,
        orchestrator=orch,
        director=director,
        stage_processor=stage_proc,
        card_renderer=renderer,
    )

    # Monkey-patch:让 _handle_action 慢 5s
    original_handle_action = gw._handle_action

    def slow_handle_action(req, cs_id):
        time.sleep(5)  # 模拟 5s 业务处理(实际 Matrix 是 80-200s)
        return original_handle_action(req, cs_id)

    gw._handle_action = slow_handle_action

    # 先建一个 session 到 PROPOSAL_READY
    cs_id = store.create_session(
        "ou_slow_test",
        {
            "current_stage": "PROPOSAL_READY",
            "music_spec": {"bpm": 88, "key": "C", "genre": "indie", "instruments": ["piano"]},
            "current_proposal": {
                "lyrics": "[verse]\ntest\n[chorus]\nstill test\n",
                "music_spec_snapshot": {"bpm": 88, "key": "C"},
            },
        },
    )["creative_session_id"]

    LarkWebhookHandler.gateway = gw
    LarkWebhookHandler.sender = sender

    server = ThreadingHTTPServer(("127.0.0.1", port), LarkWebhookHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        # 发 proposal.confirm 事件
        event = {
            "schema": "2.0",
            "header": {"event_type": "card.action.trigger_v1"},
            "event": {
                "operator": {"operator_id": {"open_id": "ou_slow_test"}},
                "context": {
                    "open_chat_id": "oc_slow_test",
                    "open_message_id": "om_slow_xxx",
                    "event_id": f"evt_slow_{int(time.time()*1000)}",
                },
                "action": {
                    "action_id": "proposal.confirm",
                    "value": {},
                },
            },
        }

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook",
            data=json.dumps(event).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        t0 = time.time()
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        dt = time.time() - t0

        # 1) ACK 必须在 < 1s(虽然业务要 5s)
        assert dt < 1.0, f"ACK took {dt:.3f}s (> 1s) — fast-ACK broken!"
        assert data["ok"] is True
        assert data.get("processing") is True

        # 2) 等 < 200ms 应该看到"处理中"卡(同步路径)
        time.sleep(0.2)
        assert len(sender.sent) == 1, f"expected 1 processing card, got {len(sender.sent)}"
        # sender.sent = [(chat_id, card_json, msg_id), ...]
        chat_id, processing_card, processing_msg_id = sender.sent[0]
        assert "⏳" in processing_card["header"]["title"]["content"], \
            f"expected processing card, got title: {processing_card['header']['title']}"

        # 3) 等 5s + buffer 应该看到 update_card 调用(MockLarkSender 记录)
        # update_card 在 sender.updates 列表里
        deadline = time.time() + 6.0
        while time.time() < deadline and len(sender.updates) == 0:
            time.sleep(0.1)

        assert len(sender.updates) == 1, (
            f"expected 1 update_card call within 6s, got {len(sender.updates)}. "
            f"sent={sender.sent}, updates={sender.updates}"
        )
        # update_card 用了 processing_msg_id
        assert sender.updates[0][0] == processing_msg_id, \
            f"update_card target wrong: {sender.updates[0]}"

        # 4) 没有发新卡(用 update 而不是 send)
        assert len(sender.sent) == 1, f"expected only processing card (no new card), got {len(sender.sent)}"

        print(f"\n✓ test passed:")
        print(f"  ACK: {dt*1000:.0f}ms (target: < 1000ms)")
        print(f"  Processing card sent: 1")
        print(f"  Update_card called: 1 (after {5}s wait)")
        print(f"  Total cards sent: 1 (no duplicate)")

    finally:
        server.shutdown()
        server_thread.join(timeout=2)
        import os as _os
        _os.unlink(db)
