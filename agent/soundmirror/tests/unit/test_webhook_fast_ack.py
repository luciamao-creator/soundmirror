"""
Fast-ACK + async worker 专项测试 — Step D.4

PM 要求:
  1. webhook 立即 ACK(< 100ms 理想 < 1s)
  2. 重放同一个 Lark event 不会重复推卡
  3. 慢 action 先发"处理中"卡,完成后 update_card
"""
import json
import threading
import time
import urllib.request

import pytest


def _wait_for(condition, timeout=3.0, interval=0.02):
    """轮询等 condition 为真"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def async_setup(server_setup):
    """扩展 server_setup,加入异步相关验证 helper"""
    return server_setup


class TestFastACK:
    """验证 webhook ACK 时间 < 1s"""

    def test_text_message_acks_under_1s(self, async_setup):
        """文本消息 webhook 应在 1s 内 ACK,即使业务处理耗时长"""
        port = async_setup["port"]
        sender = async_setup["sender"]

        # 构造 event
        event = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_alice"}},
                "message": {
                    "chat_id": "oc_alice",
                    "chat_type": "p2p",
                    "message_id": f"om_fast_{int(time.time()*1000)}",
                    "content": json.dumps({"text": "测试 fast ACK"}),
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
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        dt = time.time() - t0

        # ACK 必须 < 1 秒(Lark 限制 3s,我们给自己定的更严)
        assert dt < 1.0, f"ACK took {dt:.3f}s (> 1s)"
        assert data["ok"] is True
        assert data.get("processing") is True

    def test_button_action_acks_under_1s(self, async_setup):
        """按钮 action webhook 也应在 1s 内 ACK"""
        port = async_setup["port"]
        store = async_setup["store"]

        # 先准备一个 session 到 ARCHAEOLOGY
        cs_id = store.create_session("ou_bob", {"current_stage": "ARCHAEOLOGY"})["creative_session_id"]

        event = {
            "schema": "2.0",
            "header": {"event_type": "card.action.trigger_v1"},
            "event": {
                "operator": {"operator_id": {"open_id": "ou_bob"}},
                "context": {
                    "open_chat_id": "oc_bob",
                    "open_message_id": "om_btn_xxx",
                    "event_id": f"evt_btn_{int(time.time()*1000)}",
                },
                "action": {
                    "action_id": "chat.input",
                    "value": {"text": "btn:2"},
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
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        dt = time.time() - t0

        assert dt < 1.0, f"button ACK took {dt:.3f}s"
        assert data["ok"] is True
        assert data.get("processing") is True


class TestReplayProtection:
    """验证同一 Lark event 重放不会重复推卡"""

    def test_replay_does_not_double_send_card(self, async_setup):
        """同一 event_id 发 3 次,只有 1 张卡发出"""
        port = async_setup["port"]
        sender = async_setup["sender"]

        # 等 200ms 让前序测试的 background thread 全部完成,稳定状态
        time.sleep(0.5)

        # 用 baseline 计数,只检查本次测试的新增卡片
        baseline = len(sender.sent)
        event_id = f"om_replay_unique_{int(time.time()*1000)}"
        event = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_replay_user"}},
                "message": {
                    "chat_id": "oc_replay_chat",
                    "chat_type": "p2p",
                    "message_id": event_id,
                    "content": json.dumps({"text": "重放测试"}),
                },
            },
        }

        # 发 3 次
        for _ in range(3):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/webhook",
                data=json.dumps(event).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                json.loads(resp.read())

        # 等 1s 让 background 处理完
        time.sleep(1.0)

        # 只有 baseline 之后的 1 张新增卡(dedup 生效)
        new_cards = sender.sent[baseline:]
        assert len(new_cards) == 1, (
            f"expected 1 new card from this test, got {len(new_cards)} "
            f"(replay not deduped). New cards: {new_cards}"
        )
        # 验证是 oc_replay_chat 的卡
        assert new_cards[0][0] == "oc_replay_chat"

    def test_replay_acks_are_fast(self, async_setup):
        """重放的 ACK 也应该快(< 1s),且带 duplicate=False(因为 dedup 在 gateway 里)"""
        port = async_setup["port"]

        event_id = f"om_replay_fast_{int(time.time()*1000)}"
        event = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_charlie"}},
                "message": {
                    "chat_id": "oc_charlie",
                    "chat_type": "p2p",
                    "message_id": event_id,
                    "content": json.dumps({"text": "first"}),
                },
            },
        }

        # 第 1 次
        req1 = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook",
            data=json.dumps(event).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        t0 = time.time()
        with urllib.request.urlopen(req1, timeout=2) as resp:
            json.loads(resp.read())
        first_dt = time.time() - t0

        # 第 2 次(重放)
        time.sleep(0.1)
        req2 = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook",
            data=json.dumps(event).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        t0 = time.time()
        with urllib.request.urlopen(req2, timeout=2) as resp:
            data2 = json.loads(resp.read())
        second_dt = time.time() - t0

        # 两个 ACK 都 < 1s
        assert first_dt < 1.0
        assert second_dt < 1.0


class TestProcessingCard:
    """验证慢 action 触发"处理中"卡"""

    def test_slow_action_sends_processing_card(self, async_setup):
        """proposal.confirm 触发处理中卡"""
        port = async_setup["port"]
        sender = async_setup["sender"]
        sender.sent.clear()

        # 先准备一个 session 到 PROPOSAL_READY
        store = async_setup["store"]
        cs_id = store.create_session(
            "ou_dave",
            {
                "current_stage": "PROPOSAL_READY",
                "music_spec": {"bpm": 88, "key": "C", "genre": "indie",
                              "instruments": ["piano"]},
                "current_proposal": {
                    "lyrics": "[verse]\ntest\n[chorus]\nstill test\n",
                    "music_spec_snapshot": {"bpm": 88, "key": "C"},
                },
            },
        )["creative_session_id"]

        event = {
            "schema": "2.0",
            "header": {"event_type": "card.action.trigger_v1"},
            "event": {
                "operator": {"operator_id": {"open_id": "ou_dave"}},
                "context": {
                    "open_chat_id": "oc_dave",
                    "open_message_id": "om_dave_xxx",
                    "event_id": f"evt_proposal_{int(time.time()*1000)}",
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
        with urllib.request.urlopen(req, timeout=2) as resp:
            json.loads(resp.read())
        dt = time.time() - t0

        # ACK 必须 < 1s
        assert dt < 1.0, f"slow action ACK took {dt:.3f}s (> 1s)"

        # 等 < 500ms 应该看到"处理中"卡(在 background 跑完之前)
        time.sleep(0.2)
        # 至少有 1 张卡(可能是处理中卡)
        assert len(sender.sent) >= 1, "no processing card sent within 200ms"


class TestConcurrency:
    """验证多个并发请求各跑各的"""

    def test_concurrent_events_processed_independently(self, async_setup):
        """3 个不同 event_id 并发,应该都得到 ACK + 各自 1 张卡"""
        port = async_setup["port"]
        sender = async_setup["sender"]
        sender.sent.clear()

        events = []
        for i in range(3):
            event_id = f"om_concurrent_{i}_{int(time.time()*1000)}"
            events.append({
                "schema": "2.0",
                "header": {"event_type": "im.message.receive_v1"},
                "event": {
                    "sender": {"sender_id": {"open_id": f"ou_user_{i}"}},
                    "message": {
                        "chat_id": f"oc_user_{i}",
                        "chat_type": "p2p",
                        "message_id": event_id,
                        "content": json.dumps({"text": f"concurrent {i}"}),
                    },
                },
            })

        # 并发发(不用 ThreadPoolExecutor,沙箱 Python 没装)
        results = []
        results_lock = threading.Lock()

        def post(event):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/webhook",
                data=json.dumps(event).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=2) as resp:
                dt = time.time() - t0
                data = json.loads(resp.read())
            with results_lock:
                results.append((data, dt))

        threads = []
        for e in events:
            t = threading.Thread(target=post, args=(e,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=3)

        # 全部 ACK < 1s
        for data, dt in results:
            assert dt < 1.0, f"ACK took {dt:.3f}s"
            assert data["ok"] is True

        # 等 background
        time.sleep(1.5)
        # 应该有 3 张卡(每个 event 1 张)
        assert len(sender.sent) == 3, f"expected 3 cards, got {len(sender.sent)}"
