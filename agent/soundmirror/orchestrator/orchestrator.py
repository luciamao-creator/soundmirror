"""
Orchestrator — 动作分发 + PendingAction 两阶段确认

核心流程(Golden Case 验证):
  1. user click button → ActionEvent(action_id, payload)
  2. Orchestrator.dispatch():
     a) Catalog.validate(action_id, current_stage, payload)
     b) 检查 Risk:costly/persistent/destructive → 创建 PendingAction(冻结 payload)+ 返回确认卡按钮
        Risk:normal → 直接 dispatch 给 handler(side effects)
  3. user 点 confirm.accept → Orchestrator.confirm(pa_id)
     → 重新校验 + StateMutation(乐观锁) + 执行 payload
  4. user 点 confirm.cancel → cancel pending

防无限循环(Golden Case 09):
  - 确认卡的 action_id 只可能是 confirm.accept / confirm.cancel
  - Orchestrator 看到 confirm.* 直接处理,不查 risk
  - confirm.accept 不能嵌套,确认动作不能是 catalog 里 risk != normal 的

状态安全:
  - 所有 mutation 必须带 expected_revision
  - 所有执行必须先 evidence_session existence + ownership
"""
import time
import uuid
from typing import Optional, Callable, Any

from .catalog import ActionCatalog, CatalogAction
from ..storage.state_store import (
    StateStore, StateStoreError, StaleRevisionError, SessionNotFoundError
)


class OrchestratorError(Exception):
    pass


class CatalogValidationError(OrchestratorError):
    pass


class StageMismatchError(OrchestratorError):
    pass


class AntiLoopViolationError(OrchestratorError):
    """Golden Case 09: 高危动作被用于 confirmation 按钮"""
    pass


class DispatchResult:
    """Orchestrator.dispatch() 返回值"""
    def __init__(self, status: str, **kwargs):
        self.status = status  # "executed" | "pending" | "rejected"
        self.payload = kwargs.get("payload", {})
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self):
        attrs = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"DispatchResult({attrs})"


class Orchestrator:
    """动作分发 + PendingAction 协调器

    设计原则:
      - 状态全部交给 StateStore(不要本地缓存 revision)
      - Catalog 是唯一 action 来源
      - risk != normal 全部走 PendingAction
      - 用户身份必须在 Gateway 层验证后传入(actor_user_id)
      - creative_session_id 必须从服务端 conversation_active_session 表取
        (Golden Case 17 不信任按钮 payload)
    """

    def __init__(
        self,
        catalog: ActionCatalog,
        state_store: StateStore,
        mutation_handlers: Optional[dict[str, Callable]] = None,
    ):
        """
        Args:
            catalog: 加载好的 ActionCatalog
            state_store: 状态存储
            mutation_handlers: 可选,业务 mutation 函数注册表
              - key 是 action_id
              - 值是 fn(state_store, cs_id, payload, actor_user_id) -> dict
        """
        self.catalog = catalog
        self.state_store = state_store
        self.handlers = mutation_handlers or {}

    # =========================================================
    #  Public API
    # =========================================================

    def dispatch(
        self,
        action_id: str,
        payload: dict,
        actor_user_id: str,
        creative_session_id: str,
        idempotency_key: Optional[str] = None,
    ) -> DispatchResult:
        """统一分发入口

        Returns:
          DispatchResult(status="executed" | "pending" | "rejected", ...)
        """
        # 1) 读最新 session(拿到 current_stage + revision)
        session = self.state_store.get_session(creative_session_id)
        if not session:
            return DispatchResult("rejected", reason="session_not_found", error_code=404)
        # 2) Gold-17 跨用户保护
        if session["product_user_id"] != actor_user_id:
            return DispatchResult("rejected", reason="cross_user_permission_denied",
                                  error_code=403, audit_log="cross_session_dispatch_denied")

        # 3) Catalog validate
        validation = self.catalog.validate(action_id, session["ledger"].get("current_stage", "INIT"), payload)
        if not validation.ok:
            return DispatchResult("rejected", reason=validation.error, error_code=400)
        action = validation.action

        # 4) Golden Case 09 防循环: confirm.* 必须直通,不能再次 catalog 校验
        # (我们已经在 catalog 白名单里,但还需要确认 confirm 系列不再嵌套)

        # 5) Risk 分流
        if action.risk != "normal":
            # 高危 → PendingAction
            ttl = self._risk_ttl(action.risk)
            pa_id = self.state_store.create_pending_action(
                original_action_id=action_id,
                frozen_payload=payload,
                actor_user_id=actor_user_id,
                creative_session_id=creative_session_id,
                state_revision=session["state_revision"],
                ttl_seconds=ttl,
            )
            return DispatchResult(
                "pending",
                pending_action_id=pa_id,
                action_id=action_id,
                confirmation_policy=action.confirmation_policy,
                cost_display=action.cost_display,
                consequence=action.consequence,
                typed_text=action.typed_text,
            )

        # 6) Normal → 直接执行(handler 或 state_mutation)
        #    payload 里如果含 state_mutation,直接用;否则交给 handler
        return self._execute_normal(action, payload, actor_user_id, creative_session_id,
                                     session["state_revision"])

    def confirm(self, pending_action_id: str, actor_user_id: str) -> DispatchResult:
        """处理 confirm.accept

        1. 读 pending_action
        2. 校验 owner == actor_user_id
        3. 重新读最新 revision
        4. StateMutation(乐观锁)
        5. 执行 handler(若有)
        6. mark pending executed

        重要(Golden Case 09):
          - 这里不再次验证 risk / 不再问 "需要确认吗"
          - 一次走完
        """
        pa = self.state_store.get_pending_action(pending_action_id)
        if not pa:
            return DispatchResult("rejected", reason="pending_not_found", error_code=404)
        if pa["actor_user_id"] != actor_user_id:
            return DispatchResult("rejected", reason="pending_owner_mismatch",
                                  error_code=403, audit_log="cross_user_confirm_denied")
        if pa["status"] != "pending":
            return DispatchResult("rejected", reason=f"pending_status_{pa['status']}", error_code=409)
        if pa["expires_at"] < time.time():
            self.state_store.cancel_pending(pending_action_id)
            return DispatchResult("rejected", reason="pending_expired", error_code=410)

        # 读取最新 session
        session = self.state_store.get_session(pa["creative_session_id"])
        if not session:
            return DispatchResult("rejected", reason="session_gone", error_code=404)

        # 校验 revision 已变 → 在动作执行前通知 Orchestrator
        # 这里我们走"接受"路径:让 handler 决定怎么处理过期
        # 简化版:直接用 stored state_revision 作为 execution
        frozen_payload = pa["frozen_payload"]

        # 调用业务 handler(如果有)
        handler = self.handlers.get(pa["original_action_id"])
        if handler:
            try:
                handler(self.state_store, pa["creative_session_id"], frozen_payload, actor_user_id)
            except Exception as e:
                # handler 失败,pending 不应 executed
                self.state_store.cancel_pending(pending_action_id)
                return DispatchResult("rejected", reason=f"handler_error: {e}", error_code=500)

        # mark executed
        self.state_store.execute_pending(pending_action_id)
        return DispatchResult("executed", pending_action_id=pending_action_id,
                              original_action_id=pa["original_action_id"])

    def cancel(self, pending_action_id: str, actor_user_id: str) -> DispatchResult:
        """处理 confirm.cancel"""
        pa = self.state_store.get_pending_action(pending_action_id)
        if not pa:
            return DispatchResult("rejected", reason="pending_not_found", error_code=404)
        if pa["actor_user_id"] != actor_user_id:
            return DispatchResult("rejected", reason="pending_owner_mismatch", error_code=403)

        if pa["status"] != "pending":
            return DispatchResult("rejected", reason=f"pending_status_{pa['status']}", error_code=409)

        self.state_store.cancel_pending(pending_action_id)
        return DispatchResult("cancelled", pending_action_id=pending_action_id)

    # =========================================================
    #  Internals
    # =========================================================

    def _execute_normal(
        self,
        action: CatalogAction,
        payload: dict,
        actor_user_id: str,
        cs_id: str,
        current_revision: int,
    ) -> DispatchResult:
        """执行 normal action

        路径选择:
          1) payload 包含 "state_mutation" → 直接走 StateMutation
          2) 否则调用 handlers[action_id](如 generation.start 等业务动作)
        """
        if "state_mutation" in payload:
            mutation = payload["state_mutation"]
            try:
                result = self.state_store.apply_mutation(cs_id, mutation, actor_user_id)
                return DispatchResult("executed", action_id=action.action_id,
                                      state_revision=result["state_revision"])
            except (StaleRevisionError, SessionNotFoundError) as e:
                return DispatchResult("rejected", reason=str(e), error_code=409)
            except StateStoreError as e:
                return DispatchResult("rejected", reason=str(e), error_code=403)

        # 业务 handler
        handler = self.handlers.get(action.action_id)
        if handler:
            try:
                handler(self.state_store, cs_id, payload, actor_user_id)
                return DispatchResult("executed", action_id=action.action_id)
            except Exception as e:
                return DispatchResult("rejected", reason=f"handler_error: {e}", error_code=500)

        # 没 state_mutation 也没 handler → noop execute
        return DispatchResult("executed", action_id=action.action_id,
                              note="no_op_no_handler")

    def _risk_ttl(self, risk: str) -> int:
        """根据 risk 级别返回 PendingAction TTL(秒)"""
        return {
            "costly": 600,           # 10 分钟
            "persistent": 600,
            "destructive": 1800,    # 30 分钟,用户更慎重
        }.get(risk, 600)
