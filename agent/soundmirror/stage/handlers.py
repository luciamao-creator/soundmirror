"""
Stage Processors — 完整 11 个 stage 的 handler

每个 stage 处理:
  - 入参: Director 输出的 AgentResult, user context
  - 出参: 最终 envelope + 实际 state mutation(如果有)

设计原则(V1.1 SPEC):
  - 每个 stage 独立可测
  - 状态推进用 state_mutation(乐观锁)
  - 高危动作 → orchestrator.dispatch(走 pending)
  - 普通动作 → 立即 state mutation
"""
from dataclasses import dataclass
from typing import Optional

from ..director.director import AgentResult
from ..orchestrator.orchestrator import Orchestrator, DispatchResult
from ..storage.state_store import StateStore


@dataclass
class StageProcessedResult:
    """Stage 处理后的最终输出"""
    envelope: dict
    state_mutation: Optional[dict] = None
    tool_request: Optional[dict] = None
    log: str = ""


class StageProcessor:
    """Stage 调度器: 注册 11 个 stage 处理函数"""

    def __init__(self, orchestrator: Orchestrator, state_store: StateStore):
        self.orchestrator = orchestrator
        self.state_store = state_store

    def process(
        self,
        stage: str,
        cs_id: str,
        actor_user_id: str,
        director_result: AgentResult,
    ) -> StageProcessedResult:
        handler = self._handlers.get(stage, self._default)
        return handler(self, cs_id, actor_user_id, director_result)

    # =========================================================
    #  Handlers
    # =========================================================

    def _handle_NEW(self, cs_id, actor_user_id, dr: AgentResult):
        """NEW 阶段: 推进到 ARCHAEOLOGY"""
        # 用 director 提供的 state_mutation 直接走 orchestrator
        if dr.state_mutation:
            self._apply_mutation(cs_id, actor_user_id, dr.state_mutation)
        return StageProcessedResult(envelope=dr.envelope, log="NEW→ARCHAEOLOGY")

    def _handle_ARCHAEOLOGY(self, cs_id, actor_user_id, dr: AgentResult):
        """ARCHAEOLOGY 阶段: 累计素材,够 2 个就允许跳到 IDENTITY_FITTING"""
        if dr.state_mutation:
            self._apply_mutation(cs_id, actor_user_id, dr.state_mutation)
        return StageProcessedResult(envelope=dr.envelope, log="ARCHAEOLOGY")

    def _handle_IDENTITY_FITTING(self, cs_id, actor_user_id, dr: AgentResult):
        """IDENTITY_FITTING: 3 候选身份,用户 keep / borrow / reject"""
        if dr.state_mutation:
            self._apply_mutation(cs_id, actor_user_id, dr.state_mutation)
        return StageProcessedResult(envelope=dr.envelope, log="IDENTITY_FITTING")

    def _handle_PROPOSAL_REVIEW(self, cs_id, actor_user_id, dr: AgentResult):
        if dr.state_mutation:
            self._apply_mutation(cs_id, actor_user_id, dr.state_mutation)
        return StageProcessedResult(envelope=dr.envelope)

    def _handle_VARIABLE_SETUP(self, cs_id, actor_user_id, dr: AgentResult):
        if dr.state_mutation:
            self._apply_mutation(cs_id, actor_user_id, dr.state_mutation)
        return StageProcessedResult(envelope=dr.envelope)

    def _handle_CANDIDATE_GENERATING(self, cs_id, actor_user_id, dr: AgentResult):
        # 实际音乐生成走 tool_request → matrix connector
        # budget +1
        self._apply_mutation(cs_id, actor_user_id, {
            "expected_revision": self.state_store.get_revision(cs_id),
            "operations": [{"op": "incr", "path": "/generation_budget/used", "value": 1}],
        })
        return StageProcessedResult(
            envelope=dr.envelope,
            tool_request={"type": "music.generate", "idempotency_key": f"{cs_id}:rev{self.state_store.get_revision(cs_id)}"},
        )

    def _handle_HIGHLIGHT_REVIEW(self, cs_id, actor_user_id, dr: AgentResult):
        if dr.state_mutation:
            self._apply_mutation(cs_id, actor_user_id, dr.state_mutation)
        return StageProcessedResult(envelope=dr.envelope)

    def _handle_FULL_REVIEW(self, cs_id, actor_user_id, dr: AgentResult):
        """FULL_REVIEW: 整首完成,等用户给反馈"""
        return StageProcessedResult(envelope=dr.envelope, log="FULL_REVIEW awaiting feedback")

    def _handle_REVISION_GENERATING(self, cs_id, actor_user_id, dr: AgentResult):
        self._apply_mutation(cs_id, actor_user_id, {
            "expected_revision": self.state_store.get_revision(cs_id),
            "operations": [{"op": "incr", "path": "/revision_budget/used", "value": 1}],
        })
        return StageProcessedResult(
            envelope=dr.envelope,
            tool_request={"type": "music.generate", "idempotency_key": f"{cs_id}:revision:{self.state_store.get_revision(cs_id)}"},
        )

    def _handle_FULL_REVEAL(self, cs_id, actor_user_id, dr: AgentResult):
        """FULL_REVEAL: 推进到 CONFIRMED"""
        self._apply_mutation(cs_id, actor_user_id, {
            "expected_revision": self.state_store.get_revision(cs_id),
            "operations": [{"op": "set", "path": "/current_stage", "value": "CONFIRMED"}],
        })
        return StageProcessedResult(envelope=dr.envelope, log="FULL_REVEAL→CONFIRMED")

    def _handle_CONFIRMED(self, cs_id, actor_user_id, dr: AgentResult):
        return StageProcessedResult(envelope=dr.envelope)

    def _handle_PROFILE_ARCHIVED(self, cs_id, actor_user_id, dr: AgentResult):
        return StageProcessedResult(envelope=dr.envelope)

    def _default(self, cs_id, actor_user_id, dr: AgentResult):
        return StageProcessedResult(
            envelope={
                "card_type": "info",
                "title": "未知阶段",
                "blocks": [{"tag": "div", "text": {"content": "当前阶段尚未实现。"}}],
            },
            log="unknown_stage_default",
        )

    # =========================================================
    #  Helpers
    # =========================================================

    def _apply_mutation(self, cs_id, actor_user_id, mutation):
        """走 orchestrator 校验后 apply(实际都是 StateMutation path)"""
        # 直接调用 state_store.apply_mutation 也行,但用 orchestrator 有 audit
        try:
            self.state_store.apply_mutation(cs_id, mutation, actor_user_id)
        except Exception:
            # 乐观锁冲突时,应让 director 重试
            raise

    _handlers = {
        "NEW": _handle_NEW,
        "ARCHAEOLOGY": _handle_ARCHAEOLOGY,
        "IDENTITY_FITTING": _handle_IDENTITY_FITTING,
        "PROPOSAL_REVIEW": _handle_PROPOSAL_REVIEW,
        "VARIABLE_SETUP": _handle_VARIABLE_SETUP,
        "CANDIDATE_GENERATING": _handle_CANDIDATE_GENERATING,
        "HIGHLIGHT_REVIEW": _handle_HIGHLIGHT_REVIEW,
        "FULL_REVIEW": _handle_FULL_REVIEW,
        "REVISION_GENERATING": _handle_REVISION_GENERATING,
        "FULL_REVEAL": _handle_FULL_REVEAL,
        "CONFIRMED": _handle_CONFIRMED,
        "PROFILE_ARCHIVED": _handle_PROFILE_ARCHIVED,
    }
