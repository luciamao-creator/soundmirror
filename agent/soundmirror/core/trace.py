"""Trace — 运行轨迹记录

每个 handle() 调用 = 一个 TraceStep。
记录:
  - 输入(input_event)
  - 信号提取(signal_bundle)
  - 证据评估(evidence_assessment)
  - 风险评估(risk_assessment)
  - 决策(AgentDecision)
  - 工具调用(tool_calls)
  - 状态变化前/后
  - 验证结果

持久化到 SQLite trace 表,可以 trace_id 检索完整故事。
"""
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .types import (
    AgentDecision,
    DecisionKind,
    Evidence,
    InputEvent,
    RiskSignal,
    SessionState,
    ToolCall,
    TraceStep,
)


class TraceRecorder:
    """Trace 持久化到 SQLite"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trace (
                    trace_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL NOT NULL,
                    input_json TEXT,
                    stage_before TEXT,
                    stage_after TEXT,
                    revision_before INTEGER,
                    revision_after INTEGER,
                    decision_json TEXT,
                    evidence_json TEXT,
                    risk_json TEXT,
                    tool_calls_json TEXT,
                    validation_json TEXT,
                    notes_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trace_session
                    ON trace(session_id, step_index);
            """)

    def begin(
        self,
        session_id: str,
        step_index: int,
        input_event: Optional[InputEvent],
        stage_before: str,
        revision_before: int,
    ) -> TraceStep:
        trace = TraceStep(
            trace_id=f"tr_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            step_index=step_index,
            started_at=time.time(),
            input=input_event,
            stage_before=stage_before,
            revision_before=revision_before,
        )
        return trace

    def finish(
        self,
        trace: TraceStep,
        decision: AgentDecision,
        evidence: List[Evidence],
        risk: List[RiskSignal],
        tool_calls: List[ToolCall],
        stage_after: str,
        revision_after: int,
        validation: List[Dict[str, Any]],
        notes: List[str],
    ):
        trace.finished_at = time.time()
        trace.decision = decision
        trace.evidence = evidence
        trace.risk_signals = risk
        trace.tool_calls = tool_calls
        trace.stage_after = stage_after
        trace.revision_after = revision_after
        trace.validation = validation
        trace.notes = notes

        with sqlite3.connect(self.db_path) as conn:
            # 2026-09-30 修:原来是裸 INSERT INTO trace VALUES (?,...) 只给 15 个占位符,
            # 但表有 16 列 → sqlite3.OperationalError。
            # 说明 TraceRecorder 的 SQLite 路径从来没被真正跑通过(大家都用内存版)。
            # 现在用具名列,杜绝列数/占位符不匹配。
            conn.execute(
                """
                INSERT OR REPLACE INTO trace (
                    trace_id, session_id, step_index, started_at, finished_at,
                    input_json, stage_before, stage_after,
                    revision_before, revision_after,
                    decision_json, evidence_json, risk_json,
                    tool_calls_json, validation_json, notes_json
                ) VALUES (
                    :trace_id, :session_id, :step_index, :started_at, :finished_at,
                    :input_json, :stage_before, :stage_after,
                    :revision_before, :revision_after,
                    :decision_json, :evidence_json, :risk_json,
                    :tool_calls_json, :validation_json, :notes_json
                )
                """,
                {
                    "trace_id": trace.trace_id,
                    "session_id": trace.session_id,
                    "step_index": trace.step_index,
                    "started_at": trace.started_at,
                    "finished_at": trace.finished_at,
                    "input_json": json.dumps(trace.input.to_dict()) if trace.input else None,
                    "stage_before": trace.stage_before,
                    "stage_after": trace.stage_after,
                    "revision_before": trace.revision_before,
                    "revision_after": trace.revision_after,
                    "decision_json": json.dumps(trace.decision.to_dict()) if trace.decision else None,
                    "evidence_json": json.dumps([e.to_dict() for e in trace.evidence]),
                    "risk_json": json.dumps([s.to_dict() for s in trace.risk_signals]),
                    "tool_calls_json": json.dumps([t.to_dict() for t in trace.tool_calls]),
                    "validation_json": json.dumps(trace.validation),
                    "notes_json": json.dumps(trace.notes),
                },
            )

    def get_session_trace(self, session_id: str) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT trace_id, step_index, started_at, finished_at,
                       input_json, stage_before, stage_after,
                       decision_json, evidence_json, risk_json,
                       tool_calls_json, validation_json, notes_json
                FROM trace WHERE session_id = ?
                ORDER BY step_index
                """,
                (session_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = {
                "trace_id": r[0],
                "step_index": r[1],
                "started_at": r[2],
                "finished_at": r[3],
                "input": json.loads(r[4]) if r[4] else None,
                "stage_before": r[5],
                "stage_after": r[6],
                "decision": json.loads(r[7]) if r[7] else None,
                "evidence": json.loads(r[8]) if r[8] else [],
                "risk": json.loads(r[9]) if r[9] else [],
                "tool_calls": json.loads(r[10]) if r[10] else [],
                "validation": json.loads(r[11]) if r[11] else [],
                "notes": json.loads(r[12]) if r[12] else [],
            }
            out.append(d)
        return out


class InMemoryTraceRecorder:
    """仅内存记录,用于评测集"""
    def __init__(self):
        self.traces: List[TraceStep] = []

    def begin(self, session_id, step_index, input_event, stage_before, revision_before):
        trace = TraceStep(
            trace_id=f"tr_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            step_index=step_index,
            started_at=time.time(),
            input=input_event,
            stage_before=stage_before,
            revision_before=revision_before,
        )
        return trace

    def finish(self, trace, decision, evidence, risk, tool_calls, stage_after, revision_after, validation, notes):
        trace.finished_at = time.time()
        trace.decision = decision
        trace.evidence = evidence
        trace.risk_signals = risk
        trace.tool_calls = tool_calls
        trace.stage_after = stage_after
        trace.revision_after = revision_after
        trace.validation = validation
        trace.notes = notes
        self.traces.append(trace)

    def get_session_trace(self, session_id):
        return [t.to_dict() for t in self.traces if t.session_id == session_id]
