"""Agent Core — 主循环

核心入口:`handle(input_event) -> AgentDecision`

处理一次输入事件的所有可能动作:
  1. 恢复/创建 session
  2. 抽取信号
  3. 评估当前 stage 状态(进入条件 / 完成条件)
  4. 评估证据等级
  5. 评估风险等级
  6. 选择决策(continue / ask / confirm / retry / stop / escalate)
  7. 执行(mutation / tool call)
  8. 记录 trace
  9. 返回 decision
"""
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..storage import StateStore
from ..orchestrator.orchestrator import Orchestrator
from ..orchestrator.mutation_handlers import build_mutation_handlers
from ..orchestrator.catalog import ActionCatalog

from .types import (
    AgentDecision,
    AskOption,
    DecisionKind,
    Evidence,
    EvidenceLevel,
    InputEvent,
    InputKind,
    RiskLevel,
    RiskSignal,
    SessionState,
    ToolCall,
    TraceStep,
)
from .stage_policy import (
    ALL_STAGES,
    ARCHIVE,
    ARCHAEOLOGY,
    FULL_REVIEW,
    HIGHLIGHT_REVIEW,
    IDENTITY_FITTING,
    PROPOSAL_REVIEW,
    STAGE_BY_NAME,
    STAGE_ORDER,
    VARIABLE_SETUP,
    StagePolicy,
    get_stage,
    next_stage_of,
    validate_stage_transition,
)
from .ask_rules import (
    AskEvaluation,
    evaluate_asks,
)
from .evidence import (
    EvidenceAssessment,
    assess_session,
    field_evidence_summary,
)
from .risk import (
    RiskAssessment,
    assess_risk,
)
from .signal_extractor import (
    SignalBundle,
    extract_signals,
)
from .tool_adapter import (
    MusicToolAdapter,
    MusicGenerationRequest,
    MusicResult,
    FullSongRequest,
    FullSongResult,
    LarkToolAdapter,
    SendCardRequest,
    ensure_dir,
    time_call,
)
from .music_tool_mock import (
    GenerateCandidatesRequest,
    MusicToolAdapter as MusicToolAdapterWrapped,
    MockMusicAdapter,
    make_default_music_tool,
)
from .lark_tool_mock import (
    MockLarkAdapter,
    make_default_lark_tool,
)
from .trace import (
    InMemoryTraceRecorder,
    TraceRecorder,
)


# ============================================================
#  Project session 投影
# ============================================================

def project_session(cs_id: str, store: StateStore) -> Optional[SessionState]:
    """从 state_store 读 raw session,投影为 SessionState"""
    raw = store.get_session(cs_id)
    if not raw:
        return None
    ledger = raw.get("ledger", {}) or {}
    return SessionState(
        cs_id=cs_id,
        product_user_id=raw.get("product_user_id", ""),
        chat_id=ledger.get("chat_id", ""),
        current_stage=ledger.get("current_stage", "INIT"),
        state_revision=raw.get("state_revision", 0),
        archaeology_facts=ledger.get("archaeology_facts", []) or [],
        identity_selections=ledger.get("persona_selections", []) or [],
        identity_candidates=ledger.get("identity_candidates", []) or [],
        music_spec=ledger.get("music_spec", {}) or {},
        current_proposal=ledger.get("current_proposal"),
        highlight_clips=ledger.get("highlight_clips", []) or [],
        generation_status=ledger.get("generation_status"),
        selected_highlight=ledger.get("selected_highlight"),
        full_song=None,                              # 已合并到 selected_highlight
        personal_style_profile=ledger.get("personal_style_profile"),
        generation_job_id=ledger.get("generation_job_id"),
    )


# ============================================================
#  Agent Core
# ============================================================

class AgentCore:
    """主循环"""

    def __init__(
        self,
        store: StateStore,
        music_tool: Optional[MusicToolAdapter] = None,
        lark_tool: Optional[LarkToolAdapter] = None,
        trace_db_path: Optional[str] = None,
    ):
        self.store = store
        self.music_tool = music_tool or make_default_music_tool()
        self.lark_tool = lark_tool or make_default_lark_tool()
        # Wrap music_tool for candidate generation
        if not isinstance(self.music_tool, MusicToolAdapterWrapped):
            self.music_tool_wrapped = MusicToolAdapterWrapped(self.music_tool)
        else:
            self.music_tool_wrapped = self.music_tool

        # 加载 catalog + handlers
        catalog_path = str(Path(__file__).parent.parent / "action_catalog.json")
        self.catalog = ActionCatalog(catalog_path)
        self.handlers = build_mutation_handlers(store)
        self.orchestrator = Orchestrator(self.catalog, store, mutation_handlers=self.handlers)

        # trace
        if trace_db_path:
            self.trace = TraceRecorder(trace_db_path)
        else:
            self.trace = InMemoryTraceRecorder()

        # step_index per session
        self._step_counters: Dict[str, int] = {}

    # --------------------------------------------------------
    #  Public API
    # --------------------------------------------------------

    def handle(self, input_event: InputEvent) -> AgentDecision:
        """主入口:处理一次输入事件"""
        # 0. 恢复或创建 session
        session = self._resume_or_init(input_event)

        # 1. 开始 trace
        step_index = self._step_counters.get(session.cs_id, 0)
        trace = self.trace.begin(
            session_id=session.cs_id,
            step_index=step_index,
            input_event=input_event,
            stage_before=session.current_stage,
            revision_before=session.state_revision,
        )

        # 2. 抽取信号
        signals = extract_signals(input_event, session)

        # 2.1 标准化 action_id(某些别名映射到 catalog 中的 action)
        _ACTION_ALIASES = {
            "highlight.select": "clip.select",
            "full.confirm": "review.approve",
            # profile.archive 保持原名,因为它有 high risk 的特殊处理
        }
        if signals.detected_action in _ACTION_ALIASES:
            signals.detected_action = _ACTION_ALIASES[signals.detected_action]

        # 3. 评估风险(优先,因为 forbidden 时立即 reject)
        # 把 catalog 风险等级作为参照:risk=normal 的 action 不升级到 confirm
        cat_risk_map = {
            aid: {"risk": a.risk, "confirmation_policy": a.confirmation_policy}
            for aid, a in self.catalog.actions.items()
        }
        risk = assess_risk(
            session=session,
            text=input_event.text,
            action_id=signals.detected_action,
            target_stage=None,
            catalog=cat_risk_map,
        )

        notes: List[str] = []
        validation: List[Dict[str, Any]] = []
        tool_calls: List[ToolCall] = []

        # 3.1 拒绝场景
        if risk.forbidden:
            decision = AgentDecision(
                kind=DecisionKind.STOP,
                reasoning=risk.rationale,
                risk_signals=risk.signals,
                risk_level=RiskLevel.HIGH,
                stage=session.current_stage,
                trace_id=trace.trace_id,
            )
            self._finish_trace(trace, decision, [], risk.signals, tool_calls, validation, notes)
            return decision

        # 4. 评估 ask 规则
        ask_eval = evaluate_asks(
            session=session,
            current_stage=session.current_stage,
            triggered_inputs=signals.text_signals,
            tool_failures=signals.tool_failures,
            high_risk=risk.requires_confirm,
        )

        # 5. 评估证据
        evidence = assess_session(session)

        # 6. 决定决策
        decision = self._decide(
            session=session,
            input_event=input_event,
            signals=signals,
            ask_eval=ask_eval,
            evidence=evidence,
            risk=risk,
            tool_calls=tool_calls,
            notes=notes,
            validation=validation,
        )

        # 7. 评估阶段推进(如果决策是 continue 且当前 stage 完成)
        if decision.kind == DecisionKind.CONTINUE:
            target_stage = next_stage_of(session.current_stage)
            if target_stage:
                stage_validation = validate_stage_transition(
                    session,
                    target_stage,
                )
                validation.append({
                    "kind": "stage_transition",
                    "target": stage_validation.stage,
                    "can_enter": stage_validation.can_enter,
                    "is_complete": stage_validation.is_complete,
                    "missing": stage_validation.missing_requirements,
                })
                if stage_validation.is_complete and stage_validation.can_enter:
                    # 当前 stage 已完成,推进到下一阶段
                    advance_decision = self._advance_stage(session, target_stage, notes, tool_calls)
                    decision = advance_decision

        # 8. 持久化 + 完成 trace
        new_session = project_session(session.cs_id, self.store) or session
        self._step_counters[session.cs_id] = step_index + 1
        self._finish_trace(
            trace=trace,
            decision=decision,
            evidence=list(evidence.fields.values()),
            risk=risk.signals,
            tool_calls=tool_calls,
            stage_after=new_session.current_stage,
            revision_after=new_session.state_revision,
            validation=validation,
            notes=notes,
        )

        return decision

    # --------------------------------------------------------
    #  Decision logic
    # --------------------------------------------------------

    def _decide(
        self,
        session: SessionState,
        input_event: InputEvent,
        signals: SignalBundle,
        ask_eval: AskEvaluation,
        evidence: EvidenceAssessment,
        risk: RiskAssessment,
        tool_calls: List[ToolCall],
        notes: List[str],
        validation: List[Dict[str, Any]],
    ) -> AgentDecision:
        """核心决策树"""

        # A0. 空输入 / 纯噪声 → 必须 ASK(不猜、不静默忽略)
        #     2026-09-30 修:E3-03 暴露出空输入会走到 "continue" 空转
        raw_text = (input_event.text or "").strip()
        if input_event.kind == "text" and (
            len(raw_text) == 0
            or len(raw_text) < 4
            or signals.intent in ("unclear", "unknown")
        ):
            top = ask_eval.top_rule()
            return AgentDecision(
                kind=DecisionKind.ASK,
                reasoning=(
                    f"输入信息不足(有效内容 < 4 字符或无法判定意图),不猜测"
                    f" | intent={signals.intent} len={len(raw_text)}"
                ),
                ask_options=top.options if top else [],
                missing_fields=[top.field] if top else ["*"],
                stage=session.current_stage,
            )

        # A. 用户明确 cancel
        if signals.intent == "cancel":
            return self._make_decision(
                DecisionKind.STOP,
                session=session,
                reasoning="user cancelled",
                evidence=[],
                risk=[],
                tool_calls=tool_calls,
            )

        # B. 优先处理 tool 回调(generation 完成)
        if signals.intent == "tool_result":
            return self._handle_tool_callback(
                session=session,
                input_event=input_event,
                signals=signals,
                tool_calls=tool_calls,
                notes=notes,
                validation=validation,
            )

        # C. 高风险先 confirm
        if risk.requires_confirm and not input_event.confirm_risk:
            top_ask = ask_eval.top_rule()
            return AgentDecision(
                kind=DecisionKind.CONFIRM,
                reasoning=f"高影响动作需用户确认:{risk.rationale}",
                risk_signals=risk.signals,
                risk_level=risk.overall,
                consequence="; ".join(s.rationale for s in risk.signals),
                stage=session.current_stage,
                ask_options=top_ask.options if top_ask else [],
                missing_fields=[s.field for s in risk.signals],
                trace_id="",                                # filled in by finish
            )

        # D. Ask 规则触发(只有当用户没提供实质内容时才 ASK)
        # 如果 intent 是 fact(用户给了具体内容),先 add fact,后续 handle() 会再评估
        # 如果 user 已经 confirm_risk=True,跳过 ASK_CONFIRM_RISKY
        has_substantive_content = (
            signals.intent == "fact"
            and input_event.text
            and len(input_event.text.strip()) >= 4
        )
        user_already_confirmed = input_event.confirm_risk is True
        skip_ask_rules = []
        if user_already_confirmed:
            skip_ask_rules.append("ask.confirm.always")
        if ask_eval.should_ask() and not has_substantive_content and not (input_event.selected_option or input_event.continue_yes_no is not None):
            # 过滤掉已确认的规则
            effective_rules = [r for r in ask_eval.triggered_rules if r.rule_id not in skip_ask_rules]
            if effective_rules:
                top = max(effective_rules, key=lambda r: r.priority)
                return AgentDecision(
                    kind=DecisionKind.ASK,
                    reasoning=ask_eval.reason,
                    ask_options=top.options if top else [],
                    missing_fields=[top.field] if top else [],
                    stage=session.current_stage,
                )

        # E. 用户已回答 ask(selected_option)→ 记录并推进
        if input_event.selected_option and session.current_stage in STAGE_BY_NAME:
            return self._apply_selected_option(
                session=session,
                input_event=input_event,
                signals=signals,
                tool_calls=tool_calls,
                notes=notes,
                validation=validation,
            )

        # F. 用户显式 action(按钮)
        if signals.intent == "action" and signals.detected_action:
            # 标准化 action_id(highlight.select → clip.select,因为 catalog 只列 clip.select)
            action_id = signals.detected_action
            if action_id == "highlight.select":
                action_id = "clip.select"
            return self._dispatch_action(
                session=session,
                action_id=action_id,
                payload=signals.detected_payload or {},
                input_event=input_event,
                tool_calls=tool_calls,
                notes=notes,
                validation=validation,
            )

        # G. 阶段完成 → 推进(已在 handle() 末尾处理,这里只 continue)
        # H. 文本输入(添加 fact / 实质内容)
        if signals.intent == "fact" and input_event.text:
            return self._apply_text_fact(
                session=session,
                text=input_event.text,
                notes=notes,
                validation=validation,
            )

        # I. 阶段不是 ARCHAEOLOGY → 不允许直接添加 fact,先确认阶段
        if signals.intent == "fact" and not input_event.text:
            return AgentDecision(
                kind=DecisionKind.ASK,
                reasoning="empty fact",
                stage=session.current_stage,
            )

        # J. 不确定信号 → ASK
        if signals.intent in ("unknown", "unclear"):
            top = ask_eval.top_rule()
            if top:
                return AgentDecision(
                    kind=DecisionKind.ASK,
                    reasoning=f"input unclear: {signals.text_signals}",
                    ask_options=top.options,
                    missing_fields=[top.field],
                    stage=session.current_stage,
                )

        # K. 默认:continue 但不执行动作
        return self._make_decision(
            DecisionKind.CONTINUE,
            session=session,
            reasoning="default continue",
            evidence=[],
            risk=[],
        )

    # --------------------------------------------------------
    #  Stage advance
    # --------------------------------------------------------

    def _advance_stage(
        self,
        session: SessionState,
        next_stage: str,
        notes: List[str],
        tool_calls: Optional[List[ToolCall]] = None,
    ) -> AgentDecision:
        """推进到下一阶段"""
        rev_before = session.state_revision
        try:
            self.store.apply_mutation(
                session.cs_id,
                {
                    "expected_revision": rev_before,
                    "operations": [
                        {"op": "set", "path": "/current_stage", "value": next_stage},
                    ],
                },
                actor_user_id=session.product_user_id,
            )
            notes.append(f"stage advanced: {session.current_stage} → {next_stage}")
            new_sess = project_session(session.cs_id, self.store) or session
            return self._make_decision(
                DecisionKind.CONTINUE,
                session=new_sess,
                reasoning=f"stage advanced to {next_stage}",
                evidence=[],
                risk=[],
                next_stage=next_stage,
                tool_calls=tool_calls,
            )
        except Exception as e:
            notes.append(f"stage advance failed: {e}")
            return self._make_decision(
                DecisionKind.ESCALATE,
                session=session,
                reasoning=f"stage advance failed: {e}",
                evidence=[],
                risk=[],
                tool_calls=tool_calls,
            )

    # --------------------------------------------------------
    #  Text fact
    # --------------------------------------------------------

    def _apply_text_fact(
        self,
        session: SessionState,
        text: str,
        notes: List[str],
        validation: List[Dict[str, Any]],
    ) -> AgentDecision:
        """把用户文本加入 archaeology_facts"""
        # 只有 ARCHAEOLOGY 阶段直接添加;其它阶段需要先确认方向
        if session.current_stage != "ARCHAEOLOGY":
            return AgentDecision(
                kind=DecisionKind.ASK,
                reasoning=f"current stage is {session.current_stage}, cannot add fact directly",
                stage=session.current_stage,
            )

        rev = session.state_revision
        try:
            self.store.apply_mutation(
                session.cs_id,
                {
                    "expected_revision": rev,
                    "operations": [
                        {"op": "append", "path": "/archaeology_facts",
                         "item": {"text": text, "ts": time.time()}},
                    ],
                },
                actor_user_id=session.product_user_id,
            )
            notes.append(f"added fact: {text[:50]}")
            new_sess = project_session(session.cs_id, self.store) or session
            validation.append({
                "kind": "archaeology_added",
                "count": len(new_sess.archaeology_facts or []),
            })
            return self._make_decision(
                DecisionKind.CONTINUE,
                session=new_sess,
                reasoning=f"added fact (now {len(new_sess.archaeology_facts or [])} facts)",
                evidence=[Evidence(
                    field="archaeology_facts",
                    source="user_explicit",
                    level=EvidenceLevel.STRONG,
                    text=text[:200],
                )],
                risk=[],
            )
        except Exception as e:
            notes.append(f"add fact failed: {e}")
            return self._make_decision(
                DecisionKind.ESCALATE,
                session=session,
                reasoning=f"add fact failed: {e}",
                evidence=[],
                risk=[],
                tool_calls=tool_calls,
            )

    # --------------------------------------------------------
    #  Action dispatch
    # --------------------------------------------------------

    def _dispatch_action(
        self,
        session: SessionState,
        action_id: str,
        payload: dict,
        input_event: InputEvent,
        tool_calls: List[ToolCall],
        notes: List[str],
        validation: List[Dict[str, Any]],
    ) -> AgentDecision:
        """dispatch 一个 catalog action

        Agent Core 对某些 action 有特殊处理(proposal.confirm / highlight.select /
        profile.archive),这些 action 不走 orchestrator.handlers 的 mutation handler,
        因为它们需要调 tool 并完整控制状态。
        """
        # Agent Core 直接处理的 action(不走 orchestrator handlers)
        AGENT_CORE_ACTIONS = {"proposal.confirm", "clip.select", "review.approve", "profile.archive"}

        if action_id in AGENT_CORE_ACTIONS:
            # 直接跳到下面的特殊处理
            # 先把 selected_highlight / proposal 等写入 session(对 proposal.confirm 之前)
            if action_id == "proposal.confirm":
                # 先调 orchestrator 让 variable.confirm 的产物(current_proposal)存在
                # 实际上 orchestrator 没有 proposal.confirm 的 handler,只 mutation_handlers 有
                # 所以直接调我们的 _execute_proposal_confirm
                return self._execute_proposal_confirm(
                    session=session,
                    tool_calls=tool_calls,
                    notes=notes,
                    validation=validation,
                )
            if action_id in ("clip.select", "highlight.select"):
                return self._execute_highlight_select(
                    session=session,
                    payload=payload,
                    tool_calls=tool_calls,
                    notes=notes,
                    validation=validation,
                )
            if action_id == "review.approve":
                # 推 stage 到 CONFIRMED(标准 catalog handler 处理)
                result = self.orchestrator.dispatch(
                    action_id=action_id,
                    payload=payload,
                    actor_user_id=session.product_user_id,
                    creative_session_id=session.cs_id,
                )
                notes.append(f"orchestrator.dispatch({action_id}) → {result.status}")
                if result.status == "executed":
                    new_sess = project_session(session.cs_id, self.store) or session
                    return self._make_decision(
                        DecisionKind.CONTINUE,
                        session=new_sess,
                        reasoning=f"action {action_id} executed",
                        evidence=[],
                        risk=[],
                    )
                return self._make_decision(
                    DecisionKind.ESCALATE if result.status == "rejected" else DecisionKind.CONTINUE,
                    session=session,
                    reasoning=f"action {action_id} {result.status}",
                    evidence=[],
                    risk=[],
                    tool_calls=tool_calls,
                )
            if action_id == "profile.archive":
                return self._execute_profile_archive(
                    session=session,
                    notes=notes,
                    validation=validation,
                )

        # 其它 action 走标准 orchestrator
        result = self.orchestrator.dispatch(
            action_id=action_id,
            payload=payload,
            actor_user_id=session.product_user_id,
            creative_session_id=session.cs_id,
        )
        notes.append(f"orchestrator.dispatch({action_id}) → {result.status}")

        if result.status == "pending":
            return AgentDecision(
                kind=DecisionKind.CONFIRM,
                reasoning=f"action {action_id} requires confirmation",
                stage=session.current_stage,
                risk_level="medium",
                consequence=result.consequence or "This action needs your explicit confirmation",
                ask_options=[
                    AskOption(value="confirm", label="确认"),
                    AskOption(value="cancel", label="取消", is_default=True),
                ],
                missing_fields=[f"confirm.{action_id}"],
            )

        if result.status == "rejected":
            return AgentDecision(
                kind=DecisionKind.STOP,
                reasoning=f"action rejected: {result.reason}",
                stage=session.current_stage,
                risk_signals=[RiskSignal(
                    field=f"orchestrator.{action_id}",
                    level="medium",
                    rationale=result.reason or "rejected by orchestrator",
                )],
            )

        # status == executed
        new_sess = project_session(session.cs_id, self.store) or session

        # 特殊 action:proposal.confirm 触发 tool
        if action_id == "proposal.confirm":
            return self._execute_proposal_confirm(
                session=new_sess,
                tool_calls=tool_calls,
                notes=notes,
                validation=validation,
            )

        # 特殊 action:highlight.select 触发 full song tool
        if action_id in ("highlight.select", "clip.select"):
            return self._execute_highlight_select(
                session=new_sess,
                payload=payload,
                tool_calls=tool_calls,
                notes=notes,
                validation=validation,
            )

        # 特殊 action:profile.archive
        if action_id == "profile.archive":
            return self._execute_profile_archive(
                session=new_sess,
                notes=notes,
                validation=validation,
            )

        return self._make_decision(
            DecisionKind.CONTINUE,
            session=new_sess,
            reasoning=f"action {action_id} executed",
            evidence=[],
            risk=[],
        )

    # --------------------------------------------------------
    #  proposal.confirm → 3 个候选生成
    # --------------------------------------------------------

    def _execute_proposal_confirm(
        self,
        session: SessionState,
        tool_calls: List[ToolCall],
        notes: List[str],
        validation: List[Dict[str, Any]],
    ) -> AgentDecision:
        proposal = session.current_proposal or {}
        lyrics = (proposal.get("lyrics") if proposal else "") or ""
        spec = session.music_spec or {}
        out_dir = f"/tmp/soundmirror/highlight/{session.cs_id}"
        ensure_dir(out_dir)

        # 语言检测
        has_chinese = any('\u4e00' <= c <= '\u9fff' for c in lyrics)
        language = "zh" if has_chinese else "en"

        # prompt
        bpm = spec.get("bpm", 88)
        key = spec.get("key", "C")
        genre = spec.get("genre", "indie")
        instruments = spec.get("instruments", [])
        inst_str = "、".join(instruments) if instruments else "吉他"

        if has_chinese:
            base_prompt = f"一首中文{genre}歌曲,BPM {bpm},调式 {key},配器包括 {inst_str},中文女声演唱,verse_"
        else:
            base_prompt = f"bpm={bpm}, key={key}, genre={genre}, instruments={instruments}, variant_"

        # 调 tool
        candidates_req = GenerateCandidatesRequest(
            base_request=MusicGenerationRequest(
                prompt=base_prompt,
                lyrics=lyrics,
                output_dir=out_dir,
                preview_seconds=30,
                language=language,
                bpm=bpm,
                key=key,
                genre=genre,
                instruments=instruments,
            ),
        )

        timed = time_call(self.music_tool_wrapped.generate_candidates, candidates_req)
        result = timed.result

        tool_calls.append(ToolCall(
            tool_id="music.generate_candidates",
            arguments={
                "prompt": base_prompt[:100],
                "lyrics_len": len(lyrics),
                "language": language,
                "slots": ["A", "B", "C"],
            },
            result={
                "status": result.overall_status,
                "slots": [
                    {"slot": c.slot, "status": c.status, "node_id": c.music_result.node_id if c.music_result else None}
                    for c in result.candidates
                ],
            },
            error=timed.error,
            started_at=time.time() - timed.duration_ms / 1000,
            finished_at=time.time(),
            duration_ms=timed.duration_ms,
        ))

        if timed.error:
            notes.append(f"tool error: {timed.error}")
            return AgentDecision(
                kind=DecisionKind.RETRY,
                reasoning=f"music.generate_candidates failed: {timed.error}",
                stage=session.current_stage,
                risk_signals=[RiskSignal(
                    field="tool.music.generate",
                    level="high",
                    rationale=timed.error,
                )],
            )

        # 把 candidates 写入 session
        ready_clips = []
        for c in result.candidates:
            mr = c.music_result
            if mr is None:
                ready_clips.append({
                    "slot": c.slot,
                    "status": "FAILED",
                    "is_mock": True,
                    "error": c.error,
                })
                continue
            ready_clips.append({
                "slot": c.slot,
                "status": "READY" if mr.ok else "FAILED",
                "node_id": mr.node_id,
                "preview_path": mr.preview_path,
                "url": mr.preview_url,
                "duration_ms": mr.duration_ms,
                "is_mock": mr.is_mock,
                "adapter": mr.adapter,
                "checksum_sha256": mr.checksum_sha256,
                "signed_url_expires_at": "",
                "error": mr.error,
            })

        # 更新 session
        try:
            self.store.apply_mutation(
                session.cs_id,
                {
                    "expected_revision": session.state_revision,
                    "operations": [
                        {"op": "set", "path": "/highlight_clips", "value": ready_clips},
                        {"op": "set", "path": "/generation_status", "value": result.overall_status},
                    ],
                },
                actor_user_id=session.product_user_id,
            )
        except Exception as e:
            notes.append(f"persist clips failed: {e}")
            return AgentDecision(
                kind=DecisionKind.ESCALATE,
                reasoning=f"persist clips failed: {e}",
                stage=session.current_stage,
            )

        new_sess = project_session(session.cs_id, self.store) or session

        # 2026-09-30 修(E6-02 暴露的谎报成功 bug):
        #   之前无条件推进 → 3 路全失败时也进 HIGHLIGHT_REVIEW,
        #   用户看到"可选片段"但点开全是坏的。现在按 overall_status 分流。
        overall = result.overall_status
        if overall == "FAILED":
            self.store.apply_mutation(
                session.cs_id,
                {
                    "expected_revision": new_sess.state_revision,
                    "operations": [
                        {"op": "set", "path": "/generation_status", "value": "FAILED"},
                    ],
                },
                actor_user_id=session.product_user_id,
            )
            new_sess = project_session(session.cs_id, self.store) or new_sess
            notes.append(
                "generation FAILED -> 不推进 stage,停在 PROPOSAL_REVIEW 等待用户决策"
            )
            return self._make_decision(
                DecisionKind.RETRY,
                session=new_sess,
                tool_calls=tool_calls,
                reasoning=(
                    "3 路生成全部失败,generation_status=FAILED。"
                    "停在 PROPOSAL_REVIEW,不进入 HIGHLIGHT_REVIEW(不谎报成功)。"
                    f"错误:{[c.error for c in result.candidates if c.error][:3]}"
                ),
                evidence=[],
                risk=[RiskSignal(
                    field="tool.music.generate_candidates",
                    level="high",
                    rationale="全部候选生成失败",
                )],
            )

        if overall == "READY_WITH_FAILURES":
            notes.append(
                "generation READY_WITH_FAILURES -> 推进但标记降级"
                f"(失败槽位={[c.slot for c in result.candidates if c.status == 'FAILED']})"
            )

        # 推进 stage:PROPOSAL_REVIEW → HIGHLIGHT_REVIEW
        try:
            self.store.apply_mutation(
                session.cs_id,
                {
                    "expected_revision": new_sess.state_revision,
                    "operations": [
                        {"op": "set", "path": "/current_stage", "value": "HIGHLIGHT_REVIEW"},
                    ],
                },
                actor_user_id=session.product_user_id,
            )
            new_sess = project_session(session.cs_id, self.store) or new_sess
            notes.append("stage advanced: PROPOSAL_REVIEW -> HIGHLIGHT_REVIEW")
        except Exception as e:
            notes.append(f"stage advance after proposal.confirm failed: {e}")

        return self._make_decision(
            DecisionKind.CONTINUE,
            session=new_sess,
            reasoning=f"generated {result.overall_status} clips",
            evidence=[],
            risk=[],
            tool_calls=tool_calls,
        )

    # --------------------------------------------------------
    #  highlight.select → 选 + 全曲生成
    # --------------------------------------------------------

    def _execute_highlight_select(
        self,
        session: SessionState,
        payload: dict,
        tool_calls: List[ToolCall],
        notes: List[str],
        validation: List[Dict[str, Any]],
    ) -> AgentDecision:
        slot = payload.get("slot", "A")
        clips = session.highlight_clips or []
        target = next((c for c in clips if c.get("slot") == slot), None)
        if not target or target.get("status") != "READY":
            return AgentDecision(
                kind=DecisionKind.ASK,
                reasoning=f"slot {slot} not ready",
                stage=session.current_stage,
            )

        # 记录选择
        self.store.apply_mutation(
            session.cs_id,
            {
                "expected_revision": session.state_revision,
                "operations": [
                    {"op": "set", "path": "/selected_highlight", "value": {
                        "slot": slot,
                        "node_id": target.get("node_id"),
                        "preview_path": target.get("preview_path"),
                        "url": target.get("url"),
                        "duration_ms": target.get("duration_ms"),
                        "is_mock": target.get("is_mock"),
                        "selected_at": time.time(),
                    }},
                ],
            },
            actor_user_id=session.product_user_id,
        )

        # 生成完整歌曲
        out_dir = f"/tmp/soundmirror/full/{session.cs_id}"
        ensure_dir(out_dir)
        full_req = FullSongRequest(
            node_id=target.get("node_id"),
            is_mock=target.get("is_mock"),
            output_dir=out_dir,
        )
        timed = time_call(self.music_tool.generate_full, full_req)
        full_result = timed.result
        tool_calls.append(ToolCall(
            tool_id="music.generate_full",
            arguments={"node_id": target.get("node_id"), "slot": slot, "is_mock": target.get("is_mock")},
            result={
                "ok": full_result.ok,
                "full_song_node_id": full_result.full_song_node_id,
                "duration_ms": full_result.duration_ms,
            } if full_result else None,
            error=timed.error or (full_result.error if full_result else None),
            started_at=time.time() - timed.duration_ms / 1000,
            finished_at=time.time(),
            duration_ms=timed.duration_ms,
        ))

        if timed.error or not full_result or not full_result.ok:
            return AgentDecision(
                kind=DecisionKind.RETRY,
                reasoning=f"music.generate_full failed: {timed.error or (full_result.error if full_result else 'no result')}",
                stage=session.current_stage,
                tool_calls=tool_calls,
            )

        # 写回
        self.store.apply_mutation(
            session.cs_id,
            {
                "expected_revision": session.state_revision + 1,    # bumped once already
                "operations": [
                    {"op": "set", "path": "/selected_highlight/full_song_path",
                     "value": full_result.full_song_path},
                    {"op": "set", "path": "/selected_highlight/full_song_url",
                     "value": full_result.full_song_url},
                    {"op": "set", "path": "/selected_highlight/full_song_node_id",
                     "value": full_result.full_song_node_id},
                    {"op": "set", "path": "/selected_highlight/full_song_duration_ms",
                     "value": full_result.duration_ms},
                    {"op": "set", "path": "/selected_highlight/full_song_sha256",
                     "value": full_result.checksum_sha256},
                    {"op": "set", "path": "/current_stage", "value": "FULL_REVIEW"},
                ],
            },
            actor_user_id=session.product_user_id,
        )
        new_sess = project_session(session.cs_id, self.store) or session
        notes.append("stage advanced: HIGHLIGHT_REVIEW → FULL_REVIEW")
        return self._make_decision(
            DecisionKind.CONTINUE,
            session=new_sess,
            reasoning=f"selected slot {slot}, full song generated",
            evidence=[],
            risk=[],
            tool_calls=tool_calls,
        )

    # --------------------------------------------------------
    #  profile.archive
    # --------------------------------------------------------

    def _execute_profile_archive(
        self,
        session: SessionState,
        notes: List[str],
        validation: List[Dict[str, Any]],
    ) -> AgentDecision:
        profile = {
            "archaeology_facts": session.archaeology_facts or [],
            "persona_selections": session.identity_selections or [],
            "music_spec": session.music_spec or {},
            "selected_highlight": session.selected_highlight or {},
            "full_song": (session.selected_highlight or {}),
            "archived_at": time.time(),
        }
        self.store.apply_mutation(
            session.cs_id,
            {
                "expected_revision": session.state_revision,
                "operations": [
                    {"op": "set", "path": "/personal_style_profile", "value": profile},
                    {"op": "set", "path": "/current_stage", "value": "ARCHIVE"},
                ],
            },
            actor_user_id=session.product_user_id,
        )
        notes.append("personal_style_profile archived, stage → ARCHIVE")
        new_sess = project_session(session.cs_id, self.store) or session
        return self._make_decision(
            DecisionKind.CONTINUE,
            session=new_sess,
            reasoning="profile archived",
            evidence=[],
            risk=[],
        )

    # --------------------------------------------------------
    #  Tool callback
    # --------------------------------------------------------

    def _handle_tool_callback(
        self,
        session: SessionState,
        input_event: InputEvent,
        signals: SignalBundle,
        tool_calls: List[ToolCall],
        notes: List[str],
        validation: List[Dict[str, Any]],
    ) -> AgentDecision:
        """处理 tool_callback(同步模式通常不回调,保留扩展)"""
        # 同步 mock 模式下,tool 在 _execute_* 中已经执行完
        # 这里只对异步 / 真实 tool 模式有意义
        return self._make_decision(
            DecisionKind.CONTINUE,
            session=session,
            reasoning="tool callback handled (sync mode, no-op)",
            evidence=[],
            risk=[],
        )

    # --------------------------------------------------------
    #  Selected option(回答 ask)
    # --------------------------------------------------------

    def _apply_selected_option(
        self,
        session: SessionState,
        input_event: InputEvent,
        signals: SignalBundle,
        tool_calls: List[ToolCall],
        notes: List[str],
        validation: List[Dict[str, Any]],
    ) -> AgentDecision:
        """用户回答了 ask 的选项"""
        opt = input_event.selected_option
        if not opt:
            return self._make_decision(
                DecisionKind.ASK,
                session=session,
                reasoning="missing selected_option",
                evidence=[],
                risk=[],
                tool_calls=tool_calls,
            )

        # 简单分支:
        if opt == "够了":
            # 用户说够了 → 等同于 archaeology.confirm
            return self._dispatch_action(
                session=session,
                action_id="archaeology.confirm",
                payload={},
                input_event=input_event,
                tool_calls=tool_calls,
                notes=notes,
                validation=validation,
            )

        if opt == "引导":
            return AgentDecision(
                kind=DecisionKind.ASK,
                reasoning="user wants guidance,ask archaeology.first_fact",
                stage=session.current_stage,
                missing_fields=["archaeology_facts"],
            )

        if opt == "例子":
            return AgentDecision(
                kind=DecisionKind.ASK,
                reasoning="user wants examples",
                stage=session.current_stage,
                missing_fields=["archaeology_facts"],
            )

        if opt == "用默认":
            # 用户接受默认 music_spec
            return self._dispatch_action(
                session=session,
                action_id="variable.confirm",
                payload={},
                input_event=input_event,
                tool_calls=tool_calls,
                notes=notes,
                validation=validation,
            )

        if opt == "skip":
            return self._dispatch_action(
                session=session,
                action_id="variable.confirm",
                payload={},
                input_event=input_event,
                tool_calls=tool_calls,
                notes=notes,
                validation=validation,
            )

        if opt in ("confirm", "确认"):
            return self._make_decision(
                DecisionKind.CONTINUE,
                session=session,
                reasoning="user confirmed",
                evidence=[],
                risk=[],
                tool_calls=tool_calls,
            )

        if opt in ("cancel", "取消"):
            return self._make_decision(
                DecisionKind.STOP,
                session=session,
                reasoning="user cancelled via ask",
                evidence=[],
                risk=[],
                tool_calls=tool_calls,
            )

        # 默认:当文本响应
        return self._apply_text_fact(
            session=session,
            text=opt,
            notes=notes,
            validation=validation,
        )

    # --------------------------------------------------------
    #  Helpers
    # --------------------------------------------------------

    def _resume_or_init(self, input_event: InputEvent) -> SessionState:
        """恢复或创建 session"""
        # 1. 找现有 session
        existing = self._list_sessions_for_user(input_event.actor_user_id)
        if existing:
            cs_id = existing[0]
            sess = project_session(cs_id, self.store)
            if sess:
                # 校验 chat_id
                if sess.chat_id == input_event.chat_id:
                    return sess
                # chat_id 不一致 → 重新查找匹配 chat 的 session
                for cs_id in existing:
                    s = project_session(cs_id, self.store)
                    if s and s.chat_id == input_event.chat_id:
                        return s
                # 没匹配的 → 用第一个
                return sess

        # 2. 创建新 session
        result = self.store.create_session(input_event.actor_user_id, {
            "current_stage": "ARCHAEOLOGY",
            "chat_id": input_event.chat_id,
        })
        cs_id = result["creative_session_id"]
        return project_session(cs_id, self.store) or SessionState(
            cs_id=cs_id,
            product_user_id=input_event.actor_user_id,
            chat_id=input_event.chat_id,
            current_stage="ARCHAEOLOGY",
            state_revision=0,
        )

    def _list_sessions_for_user(self, user_id: str) -> List[str]:
        """查用户的所有未归档 session"""
        rows = self.store._list_user_sessions(user_id) if hasattr(self.store, "_list_user_sessions") else None
        if rows:
            return [r["creative_session_id"] for r in rows]
        # Fallback: SQL
        import sqlite3
        try:
            conn = sqlite3.connect(self.store.db_path)
            c = conn.execute(
                "SELECT creative_session_id FROM creative_sessions WHERE product_user_id=? AND archived=0 ORDER BY last_activity_at DESC",
                (user_id,),
            )
            ids = [r[0] for r in c.fetchall()]
            conn.close()
            return ids
        except Exception:
            return []

    def _make_decision(
        self,
        kind: str,
        session: SessionState,
        reasoning: str,
        evidence: List[Evidence],
        risk: List[RiskSignal],
        next_stage: Optional[str] = None,
        tool_calls: Optional[List[ToolCall]] = None,
    ) -> AgentDecision:
        # 2026-10-05 修:tool_calls 之前只进了 trace,没传到返回的 decision,
        # 导致接 UI 时前端拿不到"正在生成"信息,不知道该不该显示 loading。
        return AgentDecision(
            kind=kind,
            reasoning=reasoning,
            evidence=evidence,
            risk_signals=risk,
            stage=session.current_stage,
            state_revision_before=session.state_revision,
            next_stage=next_stage,
            tool_calls=list(tool_calls or []),
        )

    def _finish_trace(
        self,
        trace: TraceStep,
        decision: AgentDecision,
        evidence: List[Evidence],
        risk: List[RiskSignal],
        tool_calls: List[ToolCall],
        validation: List[Dict[str, Any]] = None,
        notes: List[str] = None,
        stage_after: Optional[str] = None,
        revision_after: Optional[int] = None,
    ):
        decision.trace_id = trace.trace_id
        # re-project to get fresh revision
        try:
            new_sess = project_session(trace.session_id, self.store)
            if new_sess:
                decision.state_revision_after = new_sess.state_revision
        except Exception:
            pass
        self.trace.finish(
            trace=trace,
            decision=decision,
            evidence=evidence,
            risk=risk,
            tool_calls=tool_calls,
            stage_after=stage_after or decision.stage,
            revision_after=revision_after if revision_after is not None else decision.state_revision_after,
            validation=validation or [],
            notes=notes or [],
        )
