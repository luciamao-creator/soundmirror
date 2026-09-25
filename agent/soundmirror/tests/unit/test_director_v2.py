"""
Director V2 (LLM) Tests
"""
import json
import pytest
import tempfile
import os
from pathlib import Path

from soundmirror.director import (
    DirectorV2, MockLLMClient, LLMMessage, LLMResult,
    make_default_llm_director,
)
from soundmirror.director import Director
from soundmirror.storage import StateStore
from soundmirror.orchestrator import ActionCatalog
from soundmirror.orchestrator.orchestrator import Orchestrator


CATALOG_PATH = str(Path(__file__).parent.parent.parent / "action_catalog.json")


@pytest.fixture
def v2_setup():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = StateStore(db_path)
    catalog = ActionCatalog(CATALOG_PATH)
    orch = Orchestrator(catalog, store)
    fallback = Director(orchestrator=orch, store=store)
    llm = MockLLMClient()
    v2 = DirectorV2(llm=llm, orchestrator=orch, store=store, fallback_director=fallback)
    yield v2, store, llm
    os.unlink(db_path)


class TestLLMClientContract:
    def test_mock_returns_llmresult(self):
        from soundmirror.director import MockLLMClient
        llm = MockLLMClient()
        result = llm.complete([LLMMessage(role="user", content="hi")])
        assert isinstance(result, LLMResult)
        assert result.content
        assert result.model

    def test_scripted_response(self):
        from soundmirror.director import MockLLMClient
        llm = MockLLMClient()
        llm.queue_response('{"envelope":{"card_type":"summary","title":"test","blocks":[],"actions":[]}}')
        result = llm.complete([LLMMessage(role="user", content="hi")])
        parsed = json.loads(result.content)
        assert parsed["envelope"]["title"] == "test"


class TestDirectorV2BasicFlow:
    def test_basic_message_returns_envelope(self, v2_setup):
        v2, store, llm = v2_setup
        cs_id = store.create_session("u_alice", {"current_stage": "ARCHAEOLOGY"})["creative_session_id"]

        result = v2.process_message("hi", "u_alice", cs_id)

        assert result.envelope is not None
        assert "card_type" in result.envelope
        # 至少调用了一次 LLM
        assert llm.call_count == 1

    def test_bpm_change_via_llm(self, v2_setup):
        v2, store, llm = v2_setup
        cs_id = store.create_session("u_alice", {"current_stage": "VARIABLE_SETUP"})["creative_session_id"]

        result = v2.process_message("BPM 88 怎么样", "u_alice", cs_id)

        # LLM 检测到 BPM
        assert "bpm" in result.envelope.get("title", "").lower() or "88" in result.envelope.get("title", "")
        assert result.state_mutation is not None
        # 不应走 fallback
        assert not result.used_fallback

    def test_genre_change_via_llm(self, v2_setup):
        v2, store, llm = v2_setup
        cs_id = store.create_session("u_alice", {"current_stage": "VARIABLE_SETUP"})["creative_session_id"]

        result = v2.process_message("换成电子", "u_alice", cs_id)
        assert "电子" in result.envelope.get("title", "") or result.state_mutation is not None


class TestDirectorV2Fallback:
    def test_fallback_on_invalid_json(self, v2_setup):
        v2, store, llm = v2_setup
        cs_id = store.create_session("u_alice", {"current_stage": "ARCHAEOLOGY"})["creative_session_id"]

        llm.queue_response("this is not JSON at all")
        result = v2.process_message("anything", "u_alice", cs_id)

        # Fallback 触发
        assert result.used_fallback is True
        assert "fallback" in result.reasoning

    def test_fallback_on_missing_envelope(self, v2_setup):
        v2, store, llm = v2_setup
        cs_id = store.create_session("u_alice", {"current_stage": "ARCHAEOLOGY"})["creative_session_id"]

        # 返回 valid JSON 但缺 envelope
        llm.queue_response('{"state_mutation": null, "tool_request": null}')
        result = v2.process_message("test", "u_alice", cs_id)
        assert result.used_fallback is True

    def test_fallback_on_button_missing_action_id(self, v2_setup):
        v2, store, llm = v2_setup
        cs_id = store.create_session("u_alice", {"current_stage": "ARCHAEOLOGY"})["creative_session_id"]

        # 返回 envelope 但 button 没有 action_id
        bad_envelope = {
            "card_type": "summary",
            "title": "test",
            "blocks": [],
            "actions": [{"text": {"content": "btn"}, "value": {}}],  # 缺 action_id
        }
        llm.queue_response(json.dumps({"envelope": bad_envelope}))
        result = v2.process_message("test", "u_alice", cs_id)
        assert result.used_fallback is True


class TestDirectorV2History:
    def test_history_included(self, v2_setup):
        v2, store, llm = v2_setup
        cs_id = store.create_session("u_alice", {"current_stage": "ARCHAEOLOGY"})["creative_session_id"]

        history = [
            LLMMessage(role="user", content="之前"),
            LLMMessage(role="assistant", content="好的"),
        ]
        result = v2.process_message("现在呢", "u_alice", cs_id, history=history)
        # history 在 messages 里
        assert any(m.content == "之前" for m in llm.last_messages)
        assert any(m.content == "现在呢" for m in llm.last_messages)


class TestDefaultFactory:
    def test_factory_returns_working_director(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        store = StateStore(db_path)
        catalog = ActionCatalog(CATALOG_PATH)
        orch = Orchestrator(catalog, store)

        director = make_default_llm_director(orchestrator=orch, store=store)

        cs_id = store.create_session("u_test", {"current_stage": "ARCHAEOLOGY"})["creative_session_id"]
        result = director.process_message("hi", "u_test", cs_id)
        assert result.envelope is not None
        os.unlink(db_path)
