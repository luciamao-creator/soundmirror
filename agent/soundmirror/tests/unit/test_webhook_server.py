"""
Webhook Server Tests
"""
import json
import time
import urllib.request

from soundmirror.gateway import (
    LarkWebhookHandler,
    verify_lark_challenge,
    verify_lark_signature,
)


class TestURLVerify:
    def test_url_verify_returns_challenge(self, server_setup):
        port = server_setup["port"]
        payload = {"challenge": "test_challenge_123"}
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook/url_verify",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        assert data["challenge"] == "test_challenge_123"


class TestHealthCheck:
    def test_health(self, server_setup):
        port = server_setup["port"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            data = json.loads(resp.read())
        assert data["status"] == "ok"


class TestMessageEvent:
    def test_text_message_returns_card(self, server_setup):
        port = server_setup["port"]
        sender = server_setup["sender"]
        sender.sent.clear()

        event = {
            "schema": "2.0",
            "header": {
                "event_type": "im.message.receive_v1",
                "app_id": "cli_xxx",
                "tenant_key": "t_xxx",
            },
            "event": {
                "sender": {"sender_id": {"open_id": "ou_alice"}},
                "message": {
                    "chat_id": "oc_test_alice",
                    "chat_type": "p2p",
                    "message_id": "om_msg_xxx",
                    "content": json.dumps({"text": "hi"}),
                },
            },
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook",
            data=json.dumps(event).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        assert data["ok"] is True
        # 2026-09-21 (Step D.4): 立即 ACK 不再带 message_id,在后台线程异步推卡
        assert data.get("processing") is True

        # 轮询等后台线程完成推卡(测试环境 gateway 处理 < 100ms)
        import time as _time
        deadline = _time.time() + 3.0
        while _time.time() < deadline and len(sender.sent) < 1:
            _time.sleep(0.05)
        # LarkSender 收到卡
        assert len(sender.sent) == 1, f"expected 1 card sent within 3s, got {len(sender.sent)}"


class TestActionEvent:
    def test_button_action_triggers_dispatch(self, server_setup):
        port = server_setup["port"]
        sender = server_setup["sender"]
        sender.sent.clear()

        # 先创建一个 session + 推 stage
        store = server_setup["store"]
        cs_id = store.create_session("ou_alice", {"current_stage": "ARCHAEOLOGY"})["creative_session_id"]
        store.apply_mutation(cs_id, {
            "expected_revision": 0,
            "operations": [{"op": "set", "path": "/current_stage", "value": "FULL_REVIEW"}],
        }, "ou_alice")

        event = {
            "schema": "2.0",
            "header": {
                "event_type": "card.action.trigger_v1",
                "app_id": "cli_xxx",
                "tenant_key": "t_xxx",
            },
            "event": {
                "operator": {"operator_id": {"open_id": "ou_alice"}},
                "context": {
                    "open_chat_id": "oc_test_alice",
                    "open_message_id": "om_msg_xxx",
                    "event_id": "evt_action_xxx",
                },
                "action": {
                    "action_id": "revision.request",
                    "value": {"feedback": "副歌加 hook"},
                },
            },
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook",
            data=json.dumps(event).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        assert data["ok"] is True
        assert data.get("processing") is True  # 立即 ACK + async 处理

        # 2026-09-21 (Step D.4): 后台线程异步推卡,需要轮询等
        import time as _time
        deadline = _time.time() + 3.0
        while _time.time() < deadline and len(sender.sent) < 1:
            _time.sleep(0.05)
        # 高危 → 二次确认卡
        assert len(sender.sent) == 1, f"expected 1 card sent within 3s, got {len(sender.sent)}"


class TestVerifyHelpers:
    def test_challenge_verification(self):
        result = verify_lark_challenge({"challenge": "abc"}, "tok")
        assert result == "abc"

    def test_challenge_verification_no_challenge(self):
        result = verify_lark_challenge({}, "tok")
        assert result is None

    def test_signature_no_encrypt_key(self):
        result = verify_lark_signature(b"body", "ts", "nonce", "sig", "")
        assert result is True  # 没配 encrypt_key,跳过

    def test_signature_correct(self):
        body = b'{"test": true}'
        ts = "1234567890"
        nonce = "abc123"
        encrypt_key = "my_secret"
        import hashlib, base64
        msg = (ts + nonce + encrypt_key).encode() + body
        expected = base64.b64encode(hashlib.sha256(msg).digest()).decode()
        assert verify_lark_signature(body, ts, nonce, expected, encrypt_key) is True

    def test_signature_incorrect(self):
        assert verify_lark_signature(b"body", "ts", "nonce", "wrong_sig", "key") is False
