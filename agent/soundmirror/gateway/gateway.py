"""
Gateway — webhook 入站 + 全流程分发

完整链路:
  1. 飞书发消息 → /webhook
  2. Gateway 解出:user_id, chat_id, message, message_id, event_id
  3. dedup:inbox_check(event_id)
  4. 找用户的 active creative_session
  5. Director.process_message(text, user_id, cs_id)
  6. StageProcessor.process(stage, cs_id, ...) → 最终 envelope
  7. LarkCardV1Renderer.render(envelope) → Lark 卡 JSON
  8. POST 回飞书 /im/v1/messages
"""
import json
import time
from dataclasses import dataclass
from typing import Optional

from ..director.director import Director
from ..orchestrator.orchestrator import Orchestrator
from ..storage.state_store import StateStore
from .card_renderer import LarkCardV1Renderer


@dataclass
class WebhookRequest:
    """Gateway 收到的入站请求

    实际从飞书 webhook 拆出,这里简化
    """
    event_id: str                # 飞书 event_id(做去重)
    user_id: str                 # 即 product_user_id (chat-open-id 已经做过映射)
    chat_id: str                 # 飞书 chat_id
    message_id: str              # 飞书 message_id(回帖用)
    text: str                    # 用户消息
    action_id: Optional[str] = None    # 如果是按钮回调
    action_payload: Optional[dict] = None  # 按钮 value
    raw: Optional[dict] = None


@dataclass
class GatewayResponse:
    """Gateway 返回值"""
    status: str                  # "ok" | "duplicate" | "error"
    response_card: Optional[dict] = None  # 如果是发新卡
    update_message_id: Optional[str] = None  # 如果是更新现有消息
    text_reply: Optional[str] = None  # 如果是纯文本回复
    error: Optional[str] = None


class Gateway:
    """Gateway 主类"""

    def __init__(
        self,
        state_store: StateStore,
        orchestrator: Orchestrator,
        director: Director,
        stage_processor,
        card_renderer: Optional[LarkCardV1Renderer] = None,
        session_resolver=None,  # Callable[(user_id, chat_id) -> session_id],实际场景注入
    ):
        """
        Args:
            state_store: 状态
            orchestrator: 动作分发
            director: 决策(输出 AgentResult)
            stage_processor: 阶段处理(输出 StageProcessedResult)
            card_renderer: 卡片渲染
            session_resolver: 把 user_id + chat_id 解析成 active cs_id
        """
        self.store = state_store
        self.orchestrator = orchestrator
        self.director = director
        self.stage = stage_processor
        self.renderer = card_renderer or LarkCardV1Renderer()
        self.session_resolver = session_resolver or self._default_session_resolver

    def _default_session_resolver(self, user_id: str, chat_id: str) -> Optional[str]:
        """简化:M1 demo 用,固定返回一个 default session"""
        # 实际通过 conversation_active_session 表查
        sessions = self._list_user_sessions(user_id)
        if sessions:
            return sessions[0]["creative_session_id"]
        # 没有就创建
        result = self.store.create_session(user_id, {
            "current_stage": "NEW",
            "chat_id": chat_id,
        })
        return result["creative_session_id"]

    def _list_user_sessions(self, user_id: str) -> list[dict]:
        """V1 implementation: 用 SQL 查 owned sessions"""
        # SQLite wrap
        import sqlite3
        conn = sqlite3.connect(self.store.db_path)
        try:
            cursor = conn.execute(
                "SELECT creative_session_id, state_revision, ledger, started_at, last_activity_at "
                "FROM creative_sessions WHERE product_user_id = ? AND archived = 0 "
                "ORDER BY last_activity_at DESC LIMIT 10",
                (user_id,),
            )
            results = []
            for row in cursor.fetchall():
                import json
                results.append({
                    "creative_session_id": row[0],
                    "state_revision": row[1],
                    "ledger": json.loads(row[2]),
                    "started_at": row[3],
                    "last_activity_at": row[4],
                })
            return results
        finally:
            conn.close()

    # =========================================================
    #  Public API
    # =========================================================

    def handle_webhook(self, req: WebhookRequest) -> GatewayResponse:
        """主入口:处理一次 webhook"""
        try:
            # 1) dedup — 把 raw_payload 存到 inbox,bridge 接管时可重建请求
            raw_payload_str = json.dumps(req.raw, ensure_ascii=False) if req.raw else None
            inbox = self.store.inbox_check(req.event_id, raw_payload=raw_payload_str)
            if inbox.is_duplicate:
                return GatewayResponse(status="duplicate")

            # 2) 找 session
            cs_id = self.session_resolver(req.user_id, req.chat_id)
            if not cs_id:
                return GatewayResponse(
                    status="error",
                    error="no_active_session",
                )

            # 3) 处理
            if req.action_id:
                return self._handle_action(req, cs_id)
            return self._handle_text(req, cs_id)
        except Exception as e:
            return GatewayResponse(status="error", error=f"unexpected: {e}")

    def _handle_text(self, req: WebhookRequest, cs_id: str) -> GatewayResponse:
        """处理纯文本消息"""
        # Director 出 envelope + state_mutation
        result = self.director.process_message(
            text=req.text,
            actor_user_id=req.user_id,
            creative_session_id=cs_id,
        )
        current_stage = self.store.get_session(cs_id)["ledger"].get("current_stage", "ARCHAEOLOGY")

        # Stage handler 应用 state_mutation
        stage_result = self.stage.process(
            stage=current_stage,
            cs_id=cs_id,
            actor_user_id=req.user_id,
            director_result=result,
        )

        # mark processed
        self.store.inbox_mark_processed(req.event_id)

        # Render 卡
        card = self.renderer.render(stage_result.envelope)
        return GatewayResponse(status="ok", response_card=card)

    def _handle_action(self, req: WebhookRequest, cs_id: str) -> GatewayResponse:
        """处理按钮回调(action_id)"""
        action_id = req.action_id
        payload = req.action_payload or {}

        # confirm.* 是特殊 case
        if action_id == "confirm.accept":
            return self._handle_confirm_accept(req, cs_id)
        if action_id == "confirm.cancel":
            return self._handle_confirm_cancel(req, cs_id)

        # 普通 action
        result = self.orchestrator.dispatch(
            action_id=action_id,
            payload=payload,
            actor_user_id=req.user_id,
            creative_session_id=cs_id,
        )
        self.store.inbox_mark_processed(req.event_id)

        if result.status == "pending":
            # 二次确认卡
            card = self.renderer.render_confirmation_card(
                original_action={
                    "pending_action_id": result.pending_action_id,
                    "action_id": result.action_id,
                },
                consequence=result.consequence or "这个操作不可回退",
                cost=result.cost_display or "",
            )
            return GatewayResponse(status="ok", response_card=card)

        if result.status == "rejected":
            envelope = {
                "card_type": "error",
                "title": "操作被拒绝",
                "blocks": [{"tag": "div", "text": {"content": result.reason or "原因不明"}}],
            }
            return GatewayResponse(
                status="ok",
                response_card=self.renderer.render(envelope),
            )

        # status == executed
        new_stage = self.store.get_session(cs_id)["ledger"].get("current_stage", "ARCHAEOLOGY")
        # 让 director 出下一阶段的 card(而不是 generic "已执行")
        director_result = self.director.process_message(
            text="", actor_user_id=req.user_id, creative_session_id=cs_id,
        )
        if director_result.envelope:
            return GatewayResponse(
                status="ok",
                response_card=self.renderer.render(director_result.envelope),
            )
        # fallback:generic 卡
        envelope = self._build_post_action_envelope(action_id, payload, new_stage)
        return GatewayResponse(status="ok", response_card=self.renderer.render(envelope))

    def _handle_confirm_accept(self, req: WebhookRequest, cs_id: str) -> GatewayResponse:
        """处理 confirm.accept 按钮"""
        pa_id = (req.action_payload or {}).get("pending_action_id", "")
        if not pa_id:
            return GatewayResponse(status="error", error="missing_pending_action_id")

        confirm_result = self.orchestrator.confirm(pa_id, req.user_id)
        self.store.inbox_mark_processed(req.event_id)

        if confirm_result.status == "executed":
            return GatewayResponse(
                status="ok",
                text_reply=f"✓ 已执行 {confirm_result.original_action_id}",
            )
        return GatewayResponse(
            status="error",
            error=f"confirm_failed: {confirm_result.reason}",
        )

    def _handle_confirm_cancel(self, req: WebhookRequest, cs_id: str) -> GatewayResponse:
        pa_id = (req.action_payload or {}).get("pending_action_id", "")
        if not pa_id:
            return GatewayResponse(status="error", error="missing_pending_action_id")
        cancel_result = self.orchestrator.cancel(pa_id, req.user_id)
        self.store.inbox_mark_processed(req.event_id)
        return GatewayResponse(status="ok", text_reply="已取消")

    def _build_post_action_envelope(self, action_id: str, payload: dict, current_stage: str) -> dict:
        """action 执行后给用户的反馈"""
        return {
            "card_type": "summary",
            "title": "✓ 已执行",
            "blocks": [
                {"tag": "div", "text": {"content": f"动作:`{action_id}` 已完成"}},
                {"tag": "note", "content": f"当前阶段:{current_stage}"},
            ],
            "actions": [],
        }
