"""
State Store Unit Tests
对应 Golden Cases:10(stale revision),11(duplicate event),17(cross session)
"""
import pytest
import tempfile
import os

from soundmirror.storage.state_store import (
    StateStore, StaleRevisionError, SessionNotFoundError,
    DuplicateEventError, InboxResult,
)


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = StateStore(path)
    yield s
    os.unlink(path)


class TestSessionLifecycle:
    def test_create_session_returns_initial_state(self, store):
        result = store.create_session("u_alice")
        assert result["state_revision"] == 0
        assert result["ledger"] == {}
        assert result["creative_session_id"].startswith("cs_")

    def test_create_session_with_initial_ledger(self, store):
        result = store.create_session(
            "u_alice",
            initial_ledger={"current_stage": "ARCHAEOLOGY", "music_spec": {"bpm": 88}},
        )
        assert result["ledger"]["current_stage"] == "ARCHAEOLOGY"
        assert result["ledger"]["music_spec"]["bpm"] == 88

    def test_get_session_round_trip(self, store):
        created = store.create_session("u_alice", {"key": "value"})
        session = store.get_session(created["creative_session_id"])
        assert session is not None
        assert session["product_user_id"] == "u_alice"
        assert session["state_revision"] == 0
        assert session["ledger"]["key"] == "value"

    def test_get_session_not_found(self, store):
        assert store.get_session("cs_not_exists") is None


class TestApplyMutation:
    """Golden Case 10 核心"""

    def test_apply_set_operation(self, store):
        cs_id = store.create_session("u_alice")["creative_session_id"]
        result = store.apply_mutation(cs_id, {
            "expected_revision": 0,
            "operations": [{"op": "set", "path": "/music_spec/bpm", "value": 88}],
        }, "u_alice")
        assert result["state_revision"] == 1
        assert result["ledger"]["music_spec"]["bpm"] == 88

    def test_apply_incr_operation(self, store):
        cs_id = store.create_session("u_alice", {"generation_budget": {"used": 0}})["creative_session_id"]
        result = store.apply_mutation(cs_id, {
            "expected_revision": 0,
            "operations": [{"op": "incr", "path": "/generation_budget/used", "value": 1}],
        }, "u_alice")
        assert result["ledger"]["generation_budget"]["used"] == 1

    def test_apply_append_operation(self, store):
        cs_id = store.create_session("u_alice")["creative_session_id"]
        result = store.apply_mutation(cs_id, {
            "expected_revision": 0,
            "operations": [
                {"op": "append", "path": "/history", "item": {"event": "test1"}},
                {"op": "append", "path": "/history", "item": {"event": "test2"}},
            ],
        }, "u_alice")
        assert result["ledger"]["history"] == [{"event": "test1"}, {"event": "test2"}]

    def test_stale_revision_rejected(self, store):
        """Golden Case 10: expected_revision != current_revision 必须拒绝"""
        cs_id = store.create_session("u_alice")["creative_session_id"]
        # 先成功一次,revision → 1
        store.apply_mutation(cs_id, {
            "expected_revision": 0,
            "operations": [{"op": "set", "path": "/music_spec/bpm", "value": 88}],
        }, "u_alice")
        # 再用一个过期的 expected_revision
        with pytest.raises(StaleRevisionError) as exc_info:
            store.apply_mutation(cs_id, {
                "expected_revision": 0,
                "operations": [{"op": "set", "path": "/music_spec/bpm", "value": 108}],
            }, "u_alice")
        assert exc_info.value.expected == 0
        assert exc_info.value.actual == 1

    def test_stale_revision_does_not_overwrite(self, store):
        """Golden Case 10: 失败后第二次 mutation 用对的新 revision 应该成功"""
        cs_id = store.create_session("u_alice")["creative_session_id"]
        store.apply_mutation(cs_id, {
            "expected_revision": 0,
            "operations": [{"op": "set", "path": "/bpm", "value": 88}],
        }, "u_alice")
        # 过期写失败
        with pytest.raises(StaleRevisionError):
            store.apply_mutation(cs_id, {
                "expected_revision": 0,
                "operations": [{"op": "set", "path": "/bpm", "value": 200}],
            }, "u_alice")
        # 用正确 revision 1 写新值
        result = store.apply_mutation(cs_id, {
            "expected_revision": 1,
            "operations": [{"op": "set", "path": "/bpm", "value": 108}],
        }, "u_alice")
        assert result["state_revision"] == 2
        # 确认 88 没被 200 覆盖
        assert store.get_session(cs_id)["ledger"]["bpm"] == 108

    def test_session_not_found(self, store):
        with pytest.raises(SessionNotFoundError):
            store.apply_mutation("cs_missing", {
                "expected_revision": 0,
                "operations": [{"op": "set", "path": "/x", "value": 1}],
            }, "u_alice")

    def test_cross_user_write_denied(self, store):
        """Golden Case 17: actor_user_id != owner 拒绝"""
        cs_id = store.create_session("u_alice")["creative_session_id"]
        with pytest.raises(Exception) as exc_info:
            store.apply_mutation(cs_id, {
                "expected_revision": 0,
                "operations": [{"op": "set", "path": "/x", "value": 1}],
            }, "u_mallory")
        assert "Permission denied" in str(exc_info.value)

    def test_cross_user_audit_log_recorded(self, store):
        cs_id = store.create_session("u_alice")["creative_session_id"]
        try:
            store.apply_mutation(cs_id, {
                "expected_revision": 0,
                "operations": [{"op": "set", "path": "/x", "value": 1}],
            }, "u_mallory")
        except Exception:
            pass
        audit = store.read_audit(cs_id)
        assert any(a["event_type"] == "cross_session_write_denied" for a in audit)


class TestInboxDedup:
    """Golden Case 11: 重复 event_id 不产生副作用"""

    def test_first_event_processed(self, store):
        result = store.inbox_check("evt_abc123")
        assert result.is_duplicate is False

    def test_duplicate_event_detected(self, store):
        first = store.inbox_check("evt_abc123")
        second = store.inbox_check("evt_abc123")
        assert first.is_duplicate is False
        assert second.is_duplicate is True

    def test_skip_duplicate_returns_true_only_if_processed(self, store):
        """未 mark processed 的不算 duplicate"""
        store.inbox_check("evt_1")
        # mark 之前不是 skipped
        assert store.inbox_skip_duplicate("evt_1") is False
        store.inbox_mark_processed("evt_1")
        # mark 之后是 skipped
        assert store.inbox_skip_duplicate("evt_1") is True

    def test_dedup_does_not_apply_side_effects(self, store):
        """Golden Case 11 核心: dedup 后不再 apply mutation"""
        cs_id = store.create_session("u_alice", {"generation_budget": {"used": 0}})["creative_session_id"]

        # T1: 第一次
        r1 = store.inbox_check("evt_bpm_88")
        assert r1.is_duplicate is False
        store.apply_mutation(cs_id, {
            "expected_revision": 0,
            "operations": [{"op": "incr", "path": "/generation_budget/used", "value": 1}],
        }, "u_alice")
        store.inbox_mark_processed("evt_bpm_88")

        # T2: 重发
        r2 = store.inbox_check("evt_bpm_88")
        assert r2.is_duplicate is True
        # 不再 apply

        session = store.get_session(cs_id)
        assert session["ledger"]["generation_budget"]["used"] == 1  # 还是 1,不是 2


class TestPendingAction:
    """Golden Case 09: PendingAction 两阶段确认"""

    def test_create_pending_action(self, store):
        cs_id = store.create_session("u_alice")["creative_session_id"]
        pa_id = store.create_pending_action(
            original_action_id="revision.request",
            frozen_payload={"feedback": "改副歌"},
            actor_user_id="u_alice",
            creative_session_id=cs_id,
            state_revision=0,
        )
        assert pa_id.startswith("pa_")
        pa = store.get_pending_action(pa_id)
        assert pa["status"] == "pending"
        assert pa["frozen_payload"]["feedback"] == "改副歌"

    def test_execute_pending_marks_status(self, store):
        cs_id = store.create_session("u_alice")["creative_session_id"]
        pa_id = store.create_pending_action(
            "revision.request", {"feedback": "改副歌"}, "u_alice", cs_id, 0
        )
        store.execute_pending(pa_id)
        pa = store.get_pending_action(pa_id)
        assert pa["status"] == "executed"
        assert pa["executed_at"] is not None

    def test_cancel_pending_marks_status(self, store):
        cs_id = store.create_session("u_alice")["creative_session_id"]
        pa_id = store.create_pending_action(
            "revision.request", {"feedback": "改副歌"}, "u_alice", cs_id, 0
        )
        store.cancel_pending(pa_id)
        pa = store.get_pending_action(pa_id)
        assert pa["status"] == "cancelled"

    def test_double_execute_blocked(self, store):
        cs_id = store.create_session("u_alice")["creative_session_id"]
        pa_id = store.create_pending_action(
            "revision.request", {"feedback": "改副歌"}, "u_alice", cs_id, 0
        )
        store.execute_pending(pa_id)
        # 第二次 execute 应失败
        with pytest.raises(Exception) as exc_info:
            store.execute_pending(pa_id)
        assert "expected pending" in str(exc_info.value)


class TestAuditLog:
    def test_create_session_audited(self, store):
        cs_id = store.create_session("u_alice")["creative_session_id"]
        audit = store.read_audit(cs_id)
        assert len(audit) >= 1
        assert audit[0]["event_type"] == "session.create"

    def test_stale_revision_audited(self, store):
        cs_id = store.create_session("u_alice")["creative_session_id"]
        store.apply_mutation(cs_id, {
            "expected_revision": 0,
            "operations": [{"op": "set", "path": "/x", "value": 1}],
        }, "u_alice")
        try:
            store.apply_mutation(cs_id, {
                "expected_revision": 0,
                "operations": [{"op": "set", "path": "/x", "value": 2}],
            }, "u_alice")
        except StaleRevisionError:
            pass
        audit = store.read_audit(cs_id)
        assert any(a["event_type"] == "stale_revision_rejected" for a in audit)
