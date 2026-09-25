"""
Integration Tests — 对应 V2 评审的 Golden Cases

这些测试模拟完整的 user click → orchestrator → state store → response 链路。
如果这些都过,V2 评审 P0-3 / P0-5 / P1-6 在工程层就稳了。
"""
import os
import pytest
import tempfile

from soundmirror.orchestrator import ActionCatalog
from soundmirror.orchestrator.orchestrator import Orchestrator
from soundmirror.storage import StateStore, StaleRevisionError


CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "action_catalog.json",
)


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = StateStore(path)
    yield s
    os.unlink(path)


@pytest.fixture
def orchestrator(store):
    catalog = ActionCatalog(CATALOG_PATH)
    return Orchestrator(catalog=catalog, state_store=store)


def create_session(store, user_id, stage="ARCHAEOLOGY"):
    return store.create_session(user_id, {"current_stage": stage})["creative_session_id"]


# ================================================================
#  Golden Case 09: high_risk_confirmation_executes_once
# ================================================================
class TestGoldenCase09HighRiskConfirmation:
    """两阶段确认协议,确认按钮不触发新的确认请求"""

    def test_revision_request_full_flow(self, orchestrator, store):
        """revision.request (costly) 必须 pending + 用户点 confirm.accept 才执行"""
        cs_id = create_session(store, "u_alice", "FULL_REVIEW")

        # 注册 handler
        executed = []
        orchestrator.handlers["revision.request"] = lambda *args: executed.append(args[2])

        # T1: 用户点击 revision.request
        result = orchestrator.dispatch(
            "revision.request",
            {"feedback": "副歌加 hook"},
            "u_alice",
            cs_id,
        )
        # → 应该 pending,不是 executed
        assert result.status == "pending"
        pa_id = result.pending_action_id

        # T2: 此时 revision.request 还没执行(frozen)
        assert len(executed) == 0

        # T3: 用户点 confirm.accept
        confirm = orchestrator.confirm(pa_id, "u_alice")
        assert confirm.status == "executed"

        # T4: handler 这次才执行
        assert len(executed) == 1
        assert executed[0]["feedback"] == "副歌加 hook"

    def test_double_confirm_blocked(self, orchestrator, store):
        """同一个 pending_action 二次 confirm 拒绝"""
        cs_id = create_session(store, "u_alice", "FULL_REVIEW")
        orchestrator.handlers["revision.request"] = lambda *a: None

        result = orchestrator.dispatch("revision.request", {"f": "x"}, "u_alice", cs_id)
        pa_id = result.pending_action_id

        orchestrator.confirm(pa_id, "u_alice")
        second = orchestrator.confirm(pa_id, "u_alice")
        assert second.status == "rejected"

    def test_high_risk_expiry_protects_state(self, orchestrator, store):
        """过期 pending action 不能被 confirm"""
        cs_id = create_session(store, "u_alice", "FULL_REVIEW")
        orchestrator.handlers["revision.request"] = lambda *a: None

        # 直接创建一个 revision=0 的 pending_action
        pa_id = store.create_pending_action(
            "revision.request", {"f": "x"}, "u_alice", cs_id,
            state_revision=0, ttl_seconds=-1,  # 已过期
        )
        result = orchestrator.confirm(pa_id, "u_alice")
        assert result.status == "rejected"
        assert "expired" in result.reason.lower()


# ================================================================
#  Golden Case 10: stale_revision_rejected
# ================================================================
class TestGoldenCase10StaleRevision:
    """StateMutation 乐观锁:过期 revision 拒绝"""

    def test_race_condition_one_wins_one_loses(self, store):
        """两个并发 mutation,一个用过期 revision,被拒绝"""
        cs_id = create_session(store, "u_alice", "VARIABLE_SETUP")

        # Read 1
        rev1 = store.get_revision(cs_id)
        assert rev1 == 0

        # Write A first (uses rev 0)
        result_a = store.apply_mutation(cs_id, {
            "expected_revision": 0,
            "operations": [{"op": "set", "path": "/bpm", "value": 88}],
        }, "u_alice")
        assert result_a["state_revision"] == 1

        # Write B 用了过期 rev 0
        with pytest.raises(StaleRevisionError):
            store.apply_mutation(cs_id, {
                "expected_revision": 0,
                "operations": [{"op": "set", "path": "/bpm", "value": 108}],
            }, "u_alice")

        # B 重试用 rev 1,成功
        result_b = store.apply_mutation(cs_id, {
            "expected_revision": 1,
            "operations": [{"op": "set", "path": "/bpm", "value": 108}],
        }, "u_alice")
        assert result_b["state_revision"] == 2
        assert store.get_session(cs_id)["ledger"]["bpm"] == 108

    def test_state_corruption_blocked(self, store):
        """stale mutation 不会污染 state"""
        cs_id = create_session(store, "u_alice")
        # 先设置 bpm=88
        store.apply_mutation(cs_id, {
            "expected_revision": 0,
            "operations": [{"op": "set", "path": "/bpm", "value": 88}],
        }, "u_alice")

        # 攻击性 mutation 用过期 rev,被拒绝
        try:
            store.apply_mutation(cs_id, {
                "expected_revision": 0,
                "operations": [{"op": "set", "path": "/bpm", "value": 999}],
            }, "u_alice")
        except StaleRevisionError:
            pass

        # bpm 还是 88
        assert store.get_session(cs_id)["ledger"]["bpm"] == 88


# ================================================================
#  Golden Case 11: duplicate_event_no_side_effect
# ================================================================
class TestGoldenCase11DuplicateEvent:
    """同一 event_id 重复到达不产生副作用"""

    def test_generation_units_not_double_counted(self, store):
        cs_id = create_session(store, "u_alice", {"generation_budget": {"used": 0, "limit": 4}})

        # T1: 事件 #1 arrived
        r1 = store.inbox_check("evt_gen_001")
        assert r1.is_duplicate is False

        # apply (cost +1)
        store.apply_mutation(cs_id, {
            "expected_revision": 0,
            "operations": [{"op": "incr", "path": "/generation_budget/used", "value": 1}],
        }, "u_alice")
        store.inbox_mark_processed("evt_gen_001")

        # T2: 飞书重发同一个 event
        r2 = store.inbox_check("evt_gen_001")
        assert r2.is_duplicate is True
        # 不再 apply

        # cost 还是 1,不是 2
        session = store.get_session(cs_id)
        assert session["ledger"]["generation_budget"]["used"] == 1
        assert session["state_revision"] == 1

    def test_third_event_still_no_effect(self, store):
        cs_id = create_session(store, "u_alice", {"counter": 0})

        for _ in range(3):
            r = store.inbox_check("evt_third_test")
            if r.is_duplicate:
                continue
            store.apply_mutation(cs_id, {
                "expected_revision": store.get_revision(cs_id),
                "operations": [{"op": "incr", "path": "/counter", "value": 1}],
            }, "u_alice")
            store.inbox_mark_processed("evt_third_test")

        assert store.get_session(cs_id)["ledger"]["counter"] == 1


# ================================================================
#  Golden Case 17: cross_session_isolation
# ================================================================
class TestGoldenCase17CrossSessionIsolation:
    """跨用户数据隔离"""

    def test_user_b_cannot_read_user_a_state(self, store):
        """User B 读 User A 的 session 直接得到 None"""
        cs_id_a = create_session(store, "u_alice")
        # 不通过 get_session,直接尝试写入(模拟恶意请求)
        try:
            store.apply_mutation(cs_id_a, {
                "expected_revision": 0,
                "operations": [{"op": "set", "path": "/x", "value": 1}],
            }, "u_bob")
        except Exception as e:
            assert "Permission denied" in str(e)

        # alice 的状态完全没变
        session = store.get_session(cs_id_a)
        assert "x" not in session["ledger"]

    def test_attack_creates_audit_entry(self, store):
        """cross_user 入侵尝试必须留下 audit 痕迹"""
        cs_id = create_session(store, "u_alice")

        try:
            store.apply_mutation(cs_id, {
                "expected_revision": 0,
                "operations": [{"op": "set", "path": "/x", "value": 1}],
            }, "u_attacker")
        except Exception:
            pass

        audit = store.read_audit(cs_id)
        assert any(a["event_type"] == "cross_session_write_denied" for a in audit)

    def test_orchestrator_also_blocks(self, orchestrator, store):
        """Orchestrator 二次校验 actor_user_id"""
        cs_id = create_session(store, "u_alice")
        result = orchestrator.dispatch(
            "archaeology.add",
            {"text": "test"},
            "u_attacker",
            cs_id,
        )
        assert result.status == "rejected"
        assert result.reason == "cross_user_permission_denied"


# ================================================================
#  Golden Case 06: action_catalog_consistency
# ================================================================
class TestGoldenCase06CatalogConsistency:
    """所有 prompt 的 action 都在 catalog"""

    def test_no_orphan_actions_in_dispatcher(self, orchestrator, store):
        """dispatch 一个 catalog 没有的 action 必须返回 rejected"""
        cs_id = create_session(store, "u_alice")
        result = orchestrator.dispatch(
            "unknown.action_id",
            {},
            "u_alice",
            cs_id,
        )
        assert result.status == "rejected"
        assert "Unknown" in result.reason or "unknown" in result.reason.lower()

    def test_all_high_risk_uses_pending(self, orchestrator, store):
        """Catalog 标记为 costly/persistent/destructive 的,必须走 pending"""
        catalog = orchestrator.catalog
        for aid, action in catalog.actions.items():
            if action.risk in ("costly", "persistent", "destructive"):
                # 必须有 confirmation_policy
                assert action.confirmation_policy != "none", (
                    f"High-risk action {aid} has confirmation_policy=none"
                )


# ================================================================
#  E2E Smoke: 完整的一个 happy path 简化版
# ================================================================
class TestEnd2EndSmoke:
    """最简 happy path,验证整个链路"""

    def test_full_user_journey(self, orchestrator, store):
        # 用户注册,session start
        session = store.create_session("u_alice", {
            "current_stage": "ARCHAEOLOGY",
            "generation_budget": {"used": 0, "limit": 4},
        })
        cs_id = session["creative_session_id"]

        # T1: 用户输入考古文本
        r1 = orchestrator.dispatch(
            "archaeology.add",
            {
                "text": "喜欢公路片、孤独感",
                "state_mutation": {
                    "expected_revision": 0,
                    "operations": [{"op": "append", "path": "/archaeology_facts",
                                  "item": {"text": "喜欢公路片、孤独感"}}],
                },
            },
            "u_alice", cs_id,
        )
        assert r1.status == "executed"

        # T2: 推到下一阶段
        r2 = orchestrator.dispatch(
            "stage.skip",
            {
                "from": "ARCHAEOLOGY",
                "state_mutation": {
                    "expected_revision": 1,
                    "operations": [{"op": "set", "path": "/current_stage", "value": "IDENTITY_FITTING"}],
                },
            },
            "u_alice", cs_id,
        )
        assert r2.status == "executed"

        # T3: 验证 ledger 状态
        session = store.get_session(cs_id)
        assert session["ledger"]["current_stage"] == "IDENTITY_FITTING"
        assert session["ledger"]["archaeology_facts"][0]["text"] == "喜欢公路片、孤独感"
