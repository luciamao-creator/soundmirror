"""
Director V2 — 用 LLM 替代 deterministic 路由

设计:
  - LLM 输出 JSON: { envelope, state_mutation, tool_request, reasoning }
  - JSON 必须严格 schema,否则 fallback 到 V1
  - V1 regex 路由作为兜底(LLM 失败时仍能工作)

LLM 不应自行生成:
  - action_id(由 catalog 白名单强制)
  - ledger 修改(由 state_mutation 走乐观锁)

LLM 应输出:
  - envelope 内容(标题、文字、按钮 label + payload)
  - state_mutation 结构(operations + expected_revision)
  - tool_request 描述(type + params,不直接调外部 API)
"""
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .llm_client import LLMClient, LLMMessage, MockLLMClient
from .intent_recognition import detect_intent, intent_to_action
from ..storage.state_store import StateStore
from ..orchestrator.orchestrator import Orchestrator


SYSTEM_PROMPT = """你是 SoundMirror Agent 的 Director,职责是把用户的话翻译成 AgentResult JSON。

约束:
  1. 严格按 schema 输出 JSON,不要有任何额外说明。
  2. envelope 是 Lark v1 卡 JSON: {card_type, title, blocks: [...], actions: [...]}
  3. state_mutation (可选): {expected_revision, operations: [{op, path, value}]}
     - 数值必须用 current_revision(从 user 消息的 <ledger_snapshot> 取)
  4. tool_request (可选): {type: "music.generate"|"revision.generate", payload: {...}}
  5. action_id 必须从下面 36 个白名单里选:

【白名单 action_id】
session.start, session.pause, session.resume, session.reset_all,
stage.skip, stage.back, summary.request,
audio.play, lyrics.show,
archaeology.add, archaeology.confirm,
identity.play, identity.keep, identity.borrow, identity.reject, identity.none,
proposal.confirm, proposal.edit, proposal.discuss,
variable.change, variable.confirm, variable.sample,
generation.start,
clip.select, clip.feedback, clip.none,
review.approve, review.rate,
revision.request,
learning.propose, learning.confirm, learning.reject,
persona.delete, persona.update_scope,
track.delete,
budget.override

6. 模板色:NEW=indigo, ARCHAEOLOGY=blue, IDENTITY_FITTING=purple,
   VARIABLE_SETUP=blue, PROPOSAL_REVIEW=blue, FULL_REVIEW=purple,
   二次确认=yellow,错误=red,常规=blue。
7. 任何 action 的 button.value 必须包含 action_id。
8. 不允许编造 user_id / session_id / token / key。

Schema:
{
  "envelope": {...},
  "state_mutation": null | {...},
  "tool_request": null | {...},
  "no_immediate_execution": false,
  "reasoning": "..."
}
"""


@dataclass
class LLMDirectorResult:
    """LLM 输出 + 解析结果"""
    envelope: dict
    state_mutation: Optional[dict] = None
    tool_request: Optional[dict] = None
    no_immediate_execution: bool = False
    reasoning: str = ""
    raw: Optional[dict] = None
    used_fallback: bool = False
    latency_ms: int = 0


class DirectorV2:
    """LLM 驱动的 Director。

    流程:
      1. 构造 messages: system_prompt + ledger_snapshot + 用户消息 + 历史
      2. LLM.completion(messages)
      3. 解析 JSON,校验 schema
      4. 失败 → fallback 到 V1(deterministic)
    """

    def __init__(self, llm: LLMClient, orchestrator=None, store=None,
                 fallback_director=None):
        self.llm = llm
        self.orchestrator = orchestrator
        self.store = store
        self.fallback_director = fallback_director

    def process_message(
        self,
        text: str,
        actor_user_id: str,
        creative_session_id: str,
        history: Optional[list[LLMMessage]] = None,
    ) -> LLMDirectorResult:
        """处理消息,返回 LLMDirectorResult"""
        session = self.store.get_session(creative_session_id) if self.store else None
        if not session:
            # 没 session → fallback 创一个
            if self.store:
                session = self.store.create_session(actor_user_id, {"current_stage": "ARCHAEOLOGY"})
            else:
                return LLMDirectorResult(envelope={"card_type":"error","title":"无 session"})

        # 构造 messages
        messages = self._build_messages(text, session, history)

        # LLM 调用
        start = time.time()
        try:
            llm_result = self.llm.complete(messages)
            raw_json = json.loads(llm_result.content)
            latency = int((time.time() - start) * 1000)
            result = self._parse(raw_json, llm_result.raw)
            result.latency_ms = latency
            return result
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            # LLM 输出不合法 → fallback
            return self._fallback(text, actor_user_id, creative_session_id, reason=str(e))

    def _build_messages(
        self,
        text: str,
        session: dict,
        history: Optional[list[LLMMessage]],
    ) -> list[LLMMessage]:
        messages = [LLMMessage(role="system", content=SYSTEM_PROMPT)]

        # ledger 快照(让 LLM 知道当前状态)
        snapshot = self._snapshot(session)
        messages.append(LLMMessage(
            role="system",
            content=f"<ledger_snapshot>\n{snapshot}\n</ledger_snapshot>",
        ))

        if history:
            messages.extend(history)

        messages.append(LLMMessage(role="user", content=text))
        return messages

    def _snapshot(self, session: dict) -> str:
        ledger = session.get("ledger", {})
        rev = session.get("state_revision", 0)
        lines = [
            f"state_revision: {rev}",
            f"current_stage: {ledger.get('current_stage', 'NEW')}",
            f"archaeology_facts: {len(ledger.get('archaeology_facts', []))}",
            f"music_spec: {ledger.get('music_spec', {})}",
            f"persona_selections: {ledger.get('persona_selections', [])}",
            f"proposal: {ledger.get('current_proposal', None)}",
        ]
        return "\n".join(lines)

    def _parse(self, raw: dict, raw_response: Optional[dict]) -> LLMDirectorResult:
        """校验 + 解析 LLM 输出"""
        if not isinstance(raw, dict):
            raise ValueError("LLM output is not a dict")

        envelope = raw.get("envelope")
        if not isinstance(envelope, dict):
            raise ValueError("missing or invalid envelope")

        # 校验 envelope 必填
        for field_name in ("card_type", "title"):
            if field_name not in envelope:
                raise ValueError(f"envelope missing {field_name}")

        # 校验 action button.value 包含 action_id
        # 同时支持 envelope.elements[] 和 envelope.actions[] 两种格式
        buttons_to_check = []
        for elem in envelope.get("elements", []):
            if elem.get("tag") == "action":
                buttons_to_check.extend(elem.get("actions", []))
        # 兼容 LLM 直接在 envelope.actions 输出(非 Lark v1 但常见)
        if not buttons_to_check and envelope.get("actions"):
            buttons_to_check = envelope["actions"]

        for btn in buttons_to_check:
            value = btn.get("value", {})
            if "action_id" not in value:
                # 兼容没有 value 的情况(标记为错误)
                if "action_id" not in btn:
                    raise ValueError(
                        f"button missing action_id: {btn.get('text', {}).get('content', btn.get('label', '?'))}"
                    )

        return LLMDirectorResult(
            envelope=envelope,
            state_mutation=raw.get("state_mutation"),
            tool_request=raw.get("tool_request"),
            no_immediate_execution=raw.get("no_immediate_execution", False),
            reasoning=raw.get("reasoning", ""),
            raw=raw_response,
        )

    def _fallback(self, text: str, actor_user_id: str, creative_session_id: str,
                  reason: str) -> LLMDirectorResult:
        """Fallback 到 V1 deterministic"""
        if self.fallback_director:
            result = self.fallback_director.process_message(text, actor_user_id, creative_session_id)
            return LLMDirectorResult(
                envelope=result.envelope,
                state_mutation=result.state_mutation,
                tool_request=result.tool_request,
                no_immediate_execution=result.no_immediate_execution,
                reasoning=f"fallback_v1: {reason}",
                used_fallback=True,
            )

        return LLMDirectorResult(
            envelope={
                "card_type": "info",
                "title": "我没完全理解",
                "blocks": [{"tag": "div", "text": {"content": f"你说:{text[:80]}"}}],
                "actions": [],
            },
            reasoning=f"no fallback available: {reason}",
            used_fallback=True,
        )


def make_default_llm_director(orchestrator=None, store=None, fallback_director=None) -> DirectorV2:
    """工厂函数:返回默认 LLM client 的 DirectorV2

    M1 默认用 MockLLMClient(可在测试外手动换 OpenAI/Mavis)
    """
    return DirectorV2(
        llm=MockLLMClient(),
        orchestrator=orchestrator,
        store=store,
        fallback_director=fallback_director,
    )
