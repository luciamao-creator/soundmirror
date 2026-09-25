"""Risk Assessor — 风险等级评估

把"该不该擅自推进"的判断从 prompt 抽成可检查规则。
- PII / voice clone:禁止(合规)
- 不可逆动作(archive / consume quota / delete):高风险
- 生成配额消耗:中风险
- 继续追问(不消耗任何东西):低风险
"""
from dataclasses import dataclass, field
from typing import List, Optional

from .types import (
    SessionState,
    RiskSignal,
    RiskLevel,
)


# ============================================================
#  Forbidden / Sensitive Patterns
# ============================================================

PII_PATTERNS = [
    "身份证", "身份证号", "手机号", "银行卡",
    "id card", "id number", "phone number",
]

# 合规红线:voice clone / impersonation 禁止
FORBIDDEN_PATTERNS = [
    "克隆声音", "像我朋友", "模仿我", "voice clone", "impersonate",
    "用我妈的声音", "用我妈", "用妈妈的声音",
    "用我爸的声音", "用我爸", "用爸爸的声音",
    "用死去的",
    "用我妈妈的声音", "用我爸爸的声音",
]

RISKY_KEYWORDS = [
    "删除", "delete", "全部重置", "reset all",
    "归档", "archive",
]


@dataclass
class RiskAssessment:
    signals: List[RiskSignal] = field(default_factory=list)
    overall: str = RiskLevel.LOW
    forbidden: bool = False
    requires_confirm: bool = False
    rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "signals": [s.to_dict() for s in self.signals],
            "overall": self.overall,
            "forbidden": self.forbidden,
            "requires_confirm": self.requires_confirm,
            "rationale": self.rationale,
        }


# ============================================================
#  Pattern checkers
# ============================================================

def _check_forbidden_patterns(text: str) -> Optional[RiskSignal]:
    """检查是否包含禁止模式(voice clone / impersonation)"""
    text_lower = text.lower()
    for pat in FORBIDDEN_PATTERNS:
        if pat.lower() in text_lower:
            return RiskSignal(
                field="compliance.voice_clone",
                level=RiskLevel.HIGH,
                rationale=f"包含禁止模式:{pat}",
                mitigation="按 V1.2 合规规则拒绝,提示用户用其他声音描述",
            )
    return None


def _check_pii(text: str) -> Optional[RiskSignal]:
    text_lower = text.lower()
    for pat in PII_PATTERNS:
        if pat.lower() in text_lower:
            return RiskSignal(
                field="compliance.pii",
                level=RiskLevel.MEDIUM,
                rationale=f"包含敏感信息:{pat}",
                mitigation="Agent 不应保存具体数字/证件号;只保留场景描述",
            )
    return None


def _check_action_risk(
    action_id: Optional[str],
    session: SessionState,
    catalog: Optional[dict] = None,
) -> List[RiskSignal]:
    """根据 action 检查风险

    catalog 是 {action_id: {risk: "normal"|"high"|..., confirmation_policy: ...}} 映射
    如果 action 在 catalog 中 risk=normal,默认不升级
    如果 action 在 catalog 中 risk=high,提升到 high
    """
    signals = []

    if action_id == "proposal.confirm":
        # 触发生成:消耗配额 + 启动 tool
        signals.append(RiskSignal(
            field="action.consume_quota",
            level=RiskLevel.MEDIUM,
            rationale="proposal.confirm 触发 3 路音乐生成,消耗生成配额(不可撤销)",
            mitigation="失败时退还配额 / 部分成功时降级呈现",
        ))
        signals.append(RiskSignal(
            field="action.irreversible",
            level=RiskLevel.MEDIUM,
            rationale="生成一旦开始无法取消;tool 失败需用户决策",
            mitigation="tool 超时自动取消 + 配额回滚",
        ))

    elif action_id == "profile.archive":
        # 写入长期档案:不可逆 + 影响未来生成
        signals.append(RiskSignal(
            field="action.long_term_memory",
            level=RiskLevel.HIGH,
            rationale="profile.archive 写入个人风格档案,影响后续所有生成",
            mitigation="必须用户 confirm 后才写;提供撤销功能",
        ))

    elif action_id in ("session.reset_all",):
        signals.append(RiskSignal(
            field="action.destructive",
            level=RiskLevel.HIGH,
            rationale=f"{action_id} 不可逆删除当前 session 全部数据",
            mitigation="二次确认;可选导出当前档案",
        ))

    elif action_id in ("highlight.select", "clip.select"):
        # 选 highlight:用户已确认过;启动 tool 但低风险
        signals.append(RiskSignal(
            field="action.full_song_generate",
            level=RiskLevel.MEDIUM,
            rationale="选择 highlight 后会生成完整歌曲(60s+ 配额)",
        ))

    # 查 catalog:如果 action 在 catalog 中 risk=normal,不升级到 confirm
    if catalog and action_id in catalog:
        cat_risk = catalog[action_id].get("risk", "normal")
        if cat_risk == "normal" and not signals:
            # catalog 说正常,且我们没标记 → 真的不风险
            pass
        elif cat_risk == "high":
            # catalog 说高 → 提升
            signals.append(RiskSignal(
                field=f"catalog.{action_id}",
                level=RiskLevel.HIGH,
                rationale=f"action {action_id} 标记为 high risk",
            ))

    return signals


def _check_stage_risk(
    current_stage: str,
    target_stage: Optional[str],
) -> List[RiskSignal]:
    """阶段推进风险"""
    signals = []

    risky_stage_transitions = {
        ("PROPOSAL_REVIEW", "HIGHLIGHT_REVIEW"): RiskSignal(
            field="stage.consume_quota",
            level=RiskLevel.MEDIUM,
            rationale="PROPOSAL_REVIEW → HIGHLIGHT_REVIEW 触发音乐生成,消耗配额",
        ),
        ("ARCHIVE", "DONE"): RiskSignal(
            field="stage.long_term_memory",
            level=RiskLevel.HIGH,
            rationale="ARCHIVE → DONE 锁定个人风格档案,不可逆",
        ),
    }

    if target_stage:
        key = (current_stage, target_stage)
        if key in risky_stage_transitions:
            signals.append(risky_stage_transitions[key])

    return signals


def _check_state_consistency(session: SessionState) -> List[RiskSignal]:
    """检查 session 状态一致性(发现明显错误时升级)"""
    signals = []

    # 1. archaeology_facts 含空字符串
    for i, f in enumerate(session.archaeology_facts or []):
        text = f.get("text", "") if isinstance(f, dict) else str(f)
        if not text.strip():
            signals.append(RiskSignal(
                field="state.empty_archaeology",
                level=RiskLevel.MEDIUM,
                rationale=f"archaeology_facts[{i}] 是空",
                mitigation="清理空 fact 或让用户重新输入",
            ))

    # 2. highlight_clips 状态不一致
    if session.highlight_clips:
        ready_count = sum(1 for c in session.highlight_clips if c.get("status") == "READY")
        pending_count = sum(1 for c in session.highlight_clips if c.get("status") == "PENDING")
        if session.generation_status == "READY" and pending_count > 0:
            signals.append(RiskSignal(
                field="state.inconsistent_clips",
                level=RiskLevel.HIGH,
                rationale=f"gen_status=READY 但仍有 {pending_count} PENDING clip",
                mitigation="重新生成或清理 PENDING",
            ))
        if session.generation_status == "READY" and ready_count == 0:
            signals.append(RiskSignal(
                field="state.no_ready_clip",
                level=RiskLevel.HIGH,
                rationale="gen_status=READY 但 0 个 READY clip",
                mitigation="禁止进入 FULL_REVIEW",
            ))

    # 3. selected_highlight 引用的 slot 不在 highlight_clips
    h = session.selected_highlight or {}
    if h:
        slot = h.get("slot")
        clip_slots = {c.get("slot") for c in (session.highlight_clips or [])}
        if slot and slot not in clip_slots:
            signals.append(RiskSignal(
                field="state.highlight_slot_missing",
                level=RiskLevel.HIGH,
                rationale=f"selected_highlight.slot={slot} 不在 highlight_clips 中",
                mitigation="禁止进入 FULL_REVIEW",
            ))

    # 4. full_song_path 但 sha256 缺失
    if h.get("full_song_path") and not h.get("full_song_sha256"):
        signals.append(RiskSignal(
            field="state.full_song_no_checksum",
            level=RiskLevel.MEDIUM,
            rationale="full_song 已生成但未计算 sha256",
            mitigation="补算 sha256",
        ))

    return signals


# ============================================================
#  Main entry
# ============================================================

def assess_risk(
    session: SessionState,
    text: str = "",
    action_id: Optional[str] = None,
    target_stage: Optional[str] = None,
    catalog: Optional[dict] = None,
) -> RiskAssessment:
    """综合评估风险

    catalog 参数:
      传 None 时,只看 _check_action_risk 内置规则
      传 dict 时,把 catalog.risk 也考虑进去
    """
    signals: List[RiskSignal] = []

    # 1. 合规红线(最高优先级)
    forbidden_signal = _check_forbidden_patterns(text)
    if forbidden_signal:
        return RiskAssessment(
            signals=[forbidden_signal],
            overall=RiskLevel.HIGH,
            forbidden=True,
            requires_confirm=False,           # 直接拒绝
            rationale=f"forbidden:{forbidden_signal.field}",
        )

    # 2. PII
    pii_signal = _check_pii(text)
    if pii_signal:
        signals.append(pii_signal)

    # 3. 动作风险
    signals.extend(_check_action_risk(action_id, session, catalog))

    # 4. 阶段推进风险
    signals.extend(_check_stage_risk(session.current_stage, target_stage))

    # 5. 状态一致性
    signals.extend(_check_state_consistency(session))

    # 综合等级
    overall = RiskLevel.LOW
    has_high = False
    for s in signals:
        if s.level == RiskLevel.HIGH:
            overall = RiskLevel.HIGH
            has_high = True
            break
        elif s.level == RiskLevel.MEDIUM:
            overall = RiskLevel.MEDIUM

    # requires_confirm 规则:
    #   - 有 HIGH signal → 必须 confirm
    #   - 只有 MEDIUM → 看 catalog:
    #     - action 在 catalog 且 risk=normal → 不需 confirm(用户已通过 action 表达)
    #     - 否则 → confirm
    catalog_normal = False
    if catalog and action_id and action_id in catalog:
        if catalog[action_id].get("risk") == "normal":
            catalog_normal = True

    if has_high:
        requires_confirm = True
    elif catalog_normal:
        requires_confirm = False
    else:
        requires_confirm = any(s.level == RiskLevel.MEDIUM for s in signals)

    return RiskAssessment(
        signals=signals,
        overall=overall,
        forbidden=False,
        requires_confirm=requires_confirm,
        rationale=f"{len(signals)} signals; overall={overall}",
    )
