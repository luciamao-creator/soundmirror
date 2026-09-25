"""
End-to-End Smoke Test
模拟完整用户旅程:webhook → Gateway → Director → Stage → Renderer
"""
import os
import json
import pytest
import tempfile

from soundmirror.storage import StateStore
from soundmirror.orchestrator import ActionCatalog
from soundmirror.orchestrator.orchestrator import Orchestrator
from soundmirror.director import Director
from soundmirror.stage import StageProcessor
from soundmirror.gateway import LarkCardV1Renderer
from soundmirror.gateway.gateway import Gateway, WebhookRequest


CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "action_catalog.json",
)


@pytest.fixture
def gateway():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

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
    )
    yield gw, store
    os.unlink(db_path)


class TestEndToEndJourney:
    """完整的:用户消息 → 卡片返回"""

    def test_first_message_creates_session_and_returns_welcome(self, gateway):
        gw, store = gateway

        # 模拟飞书 webhook
        req = WebhookRequest(
            event_id="evt_welcome_001",
            user_id="u_alice",
            chat_id="oc_alice_chat_1",
            message_id="om_msg_001",
            text="hi",
        )
        resp = gw.handle_webhook(req)

        assert resp.status == "ok"
        assert resp.response_card is not None
        # 应该出现 NEW stage 的 onboarding 卡
        elements = resp.response_card["elements"]
        assert any("人 / 场景" in str(e) for e in elements)

        # session 已创建
        sessions = gw._list_user_sessions("u_alice")
        assert len(sessions) == 1
        # stage 已推到 ARCHAEOLOGY
        assert sessions[0]["ledger"]["current_stage"] == "ARCHAEOLOGY"

    def test_second_message_in_archaeology_adds_fact(self, gateway):
        gw, store = gateway

        # 第一次
        gw.handle_webhook(WebhookRequest(
            event_id="evt_2_001",
            user_id="u_alice",
            chat_id="oc_alice_chat_1",
            message_id="om_001",
            text="hi",
        ))

        # 第二次(用户给素材)
        resp = gw.handle_webhook(WebhookRequest(
            event_id="evt_2_002",
            user_id="u_alice",
            chat_id="oc_alice_chat_1",
            message_id="om_002",
            text="我喜欢公路片,尤其是 Joaquin Phoenix 那种孤独感",
        ))

        assert resp.status == "ok"
        # 验证 state 已写入
        cs_id = gw._list_user_sessions("u_alice")[0]["creative_session_id"]
        session = store.get_session(cs_id)
        facts = session["ledger"].get("archaeology_facts", [])
        assert len(facts) == 1
        assert "公路片" in facts[0]["text"]

    def test_button_click_routes_to_orchestrator(self, gateway):
        """用户点 '确认修订' 按钮 → orchestrator.dispatch → executed / pending"""
        gw, store = gateway

        # 进入 FULL_REVIEW 阶段
        cs_id = gw._list_user_sessions("u_alice")[0]["creative_session_id"] if gw._list_user_sessions("u_alice") else None
        if not cs_id:
            # 先创建 session
            result = store.create_session("u_alice", {"current_stage": "FULL_REVIEW"})
            cs_id = result["creative_session_id"]
        else:
            store.apply_mutation(cs_id, {
                "expected_revision": store.get_revision(cs_id),
                "operations": [{"op": "set", "path": "/current_stage", "value": "FULL_REVIEW"}],
            }, "u_alice")

        # 用户点 revise button (revision.request 是 high risk)
        resp = gw.handle_webhook(WebhookRequest(
            event_id="evt_revise_001",
            user_id="u_alice",
            chat_id="oc_alice_chat_1",
            message_id="om_rev_001",
            text="",  # 按钮回调通常 text 为空
            action_id="revision.request",
            action_payload={"feedback": "副歌加 hook"},
        ))

        # 高危 → 二次确认卡
        assert resp.status == "ok"
        assert resp.response_card is not None
        header = resp.response_card.get("header", {})
        assert "确认" in header.get("title", {}).get("content", "")

    def test_duplicate_event_returns_duplicate_status(self, gateway):
        gw, _ = gateway
        req = WebhookRequest(
            event_id="evt_dup_001",
            user_id="u_alice",
            chat_id="oc_alice_chat_1",
            message_id="om_dup",
            text="hi",
        )
        resp1 = gw.handle_webhook(req)
        resp2 = gw.handle_webhook(req)
        assert resp1.status == "ok"
        assert resp2.status == "duplicate"

    def test_full_journey_archaeology_to_full_review(self, gateway):
        """简化的完整 journey:NEW → ARCHAEOLOGY → ... → FULL_REVIEW"""
        gw, store = gateway

        # T0: 触发 welcome
        gw.handle_webhook(WebhookRequest(
            event_id="jt_0", user_id="u_alice", chat_id="oc_alice",
            message_id="om_0", text="hi",
        ))

        # T1: 加 2 段素材
        gw.handle_webhook(WebhookRequest(
            event_id="jt_1", user_id="u_alice", chat_id="oc_alice",
            message_id="om_1", text="喜欢公路感",
        ))
        gw.handle_webhook(WebhookRequest(
            event_id="jt_2", user_id="u_alice", chat_id="oc_alice",
            message_id="om_2", text="喜欢孤独",
        ))

        cs_id = gw._list_user_sessions("u_alice")[0]["creative_session_id"]
        session = store.get_session(cs_id)
        facts = session["ledger"].get("archaeology_facts", [])
        assert len(facts) == 2


class TestCardSchema:
    """Lark Card v1 输出 schema 验证"""

    def test_card_has_required_fields(self, gateway):
        gw, _ = gateway
        req = WebhookRequest(
            event_id="evt_card_001",
            user_id="u_alice",
            chat_id="oc_alice",
            message_id="om_card",
            text="hi",
        )
        resp = gw.handle_webhook(req)

        card = resp.response_card
        # v1 schema 必有
        assert "config" in card
        assert "header" in card
        assert "elements" in card
        # header 必有 title.tag = plain_text + content
        assert card["header"]["title"]["tag"] == "plain_text"
        # elements 必有内容
        assert len(card["elements"]) > 0

    def test_button_value_contains_action_id(self, gateway):
        gw, _ = gateway
        # buttons 必含 action_id 用于回调
        card_resp = gw.handle_webhook(WebhookRequest(
            event_id="evt_btn_001",
            user_id="u_alice",
            chat_id="oc_alice",
            message_id="om_btn",
            text="hi",
        ))
        # 找 button
        buttons = []
        for elem in card_resp.response_card["elements"]:
            if elem.get("tag") == "action":
                for btn in elem.get("actions", []):
                    buttons.append(btn)
        assert any("action_id" in btn.get("value", {}) for btn in buttons)


class TestRendererUnit:
    """Card renderer 直接单测"""

    def test_renders_text_block(self):
        r = LarkCardV1Renderer()
        card = r.render({
            "card_type": "summary",
            "title": "测试",
            "blocks": [{"tag": "div", "text": {"content": "hello"}}],
            "actions": [],
        })
        assert card["header"]["title"]["content"] == "测试"
        assert card["header"]["template"] == "blue"

    def test_renders_multiple_buttons(self):
        r = LarkCardV1Renderer()
        card = r.render({
            "card_type": "selection",
            "title": "选一个",
            "blocks": [{"tag": "div", "text": {"content": "options"}}],
            "actions": [
                {"action_id": "a.b", "label": "A", "value": {}},
                {"action_id": "a.c", "label": "B", "value": {"slot": "B"}},
            ],
        })
        # find action block
        action_blocks = [e for e in card["elements"] if e.get("tag") == "action"]
        assert len(action_blocks) == 1
        buttons = action_blocks[0]["actions"]
        assert len(buttons) == 2
        # button 1: action_id 在 value 里
        assert buttons[0]["value"]["action_id"] == "a.b"
        # button 2: slot payload 仍在
        assert buttons[1]["value"]["slot"] == "B"

    def test_renders_confirmation_card_distinct_color(self):
        r = LarkCardV1Renderer()
        card = r.render({
            "card_type": "confirmation",
            "title": "确认",
            "blocks": [{"tag": "div", "text": {"content": "这不可回退"}}],
            "actions": [
                {"action_id": "confirm.accept", "label": "✓ 是", "value": {}, "danger": True},
                {"action_id": "confirm.cancel", "label": "✗ 否", "value": {}},
            ],
        })
        # confirmation 卡 header 应是 yellow
        assert card["header"]["template"] == "yellow"
        # 第一个 button danger
        btn1 = next(e for e in card["elements"] if e.get("tag") == "action")["actions"][0]
        assert btn1["type"] == "danger"
