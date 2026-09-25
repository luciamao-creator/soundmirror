"""Recovery — 中断恢复

进程被 KILL 时:
- 已持久化的 session 在 SQLite
- inbox 表有 status='processing' 的孤儿

启动时:
- 检测 in-flight sessions(generation_status=GENERATING)
- 检查每个 tool 是否真的完成(看磁盘文件存在)
- 未完成的 → 自动 retry 或 escalate
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..storage import StateStore
from .types import SessionState
from .stage_policy import get_stage
from .agent_core import project_session


@dataclass
class RecoveryPlan:
    """恢复计划"""
    in_flight_sessions: List[str] = field(default_factory=list)
    orphan_inbox: List[str] = field(default_factory=list)
    recoverable_sessions: List[str] = field(default_factory=list)
    needs_user_decision: List[Dict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def detect_in_flight(store: StateStore) -> RecoveryPlan:
    """扫描 DB 找出需要恢复的状态"""
    plan = RecoveryPlan()

    import sqlite3
    conn = sqlite3.connect(store.db_path)
    try:
        # 1. orphan inbox(status=processing 且 raw_payload 有)
        rows = conn.execute(
            "SELECT event_id FROM inbox WHERE status='processing' AND raw_payload IS NOT NULL"
        ).fetchall()
        plan.orphan_inbox = [r[0] for r in rows]

        # 2. in-flight sessions
        rows = conn.execute(
            "SELECT creative_session_id FROM creative_sessions WHERE archived=0"
        ).fetchall()
        for (cs_id,) in rows:
            sess = project_session(cs_id, store)
            if not sess:
                continue
            if sess.generation_status == "GENERATING":
                plan.in_flight_sessions.append(cs_id)
                # 检查 highlight clips 是否真存在
                clips = sess.highlight_clips or []
                pending_count = sum(1 for c in clips if c.get("status") == "PENDING")
                ready_count = sum(1 for c in clips if c.get("status") == "READY")
                if pending_count > 0 and ready_count == 0:
                    # 全 PENDING,从未生成 → 可恢复
                    plan.recoverable_sessions.append(cs_id)
                    plan.notes.append(f"{cs_id}: {pending_count} PENDING clips, ready for retry")
                elif pending_count > 0 and ready_count > 0:
                    # 部分成功 → 让用户决策
                    plan.needs_user_decision.append({
                        "cs_id": cs_id,
                        "kind": "partial_generation",
                        "ready": ready_count,
                        "pending": pending_count,
                    })
    finally:
        conn.close()
    return plan


def file_exists(path: str) -> bool:
    return bool(path) and Path(path).exists()


def verify_clips_on_disk(session: SessionState) -> Dict[str, bool]:
    """验证每个 highlight clip 的文件是否在磁盘上"""
    out = {}
    for clip in (session.highlight_clips or []):
        path = clip.get("preview_path")
        out[clip.get("slot", "?")] = file_exists(path) if path else False
    return out

