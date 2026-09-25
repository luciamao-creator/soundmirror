"""Stage Policy — 7 个阶段的进入 / 完成 / 风险 / 证据规则

每个阶段是一个可检查的规则,不依赖 prompt。
"""
from dataclasses import dataclass
from typing import Callable, List, Optional

from .types import (
    SessionState,
    EvidenceLevel,
    RiskLevel,
)


# ============================================================
#  Stage Policy 数据类
# ============================================================

@dataclass
class StagePolicy:
    stage: str
    description: str
    min_evidence_level: str                              # EvidenceLevel.*
    risk_if_incomplete: str                              # RiskLevel.*
    can_enter: Callable[[SessionState], bool]            # 进入条件
    is_complete: Callable[[SessionState], bool]          # 完成条件
    next_stage: Optional[str] = None                     # 推进目标
    artifact_keys: List[str] = field(default_factory=list) if False else None  # 兼容旧版


# 修正:Python dataclass 不支持 if False else None
from dataclasses import field


@dataclass
class StagePolicy:
    stage: str
    description: str
    min_evidence_level: str                              # EvidenceLevel.*
    risk_if_incomplete: str                              # RiskLevel.*
    can_enter: Callable[[SessionState], bool]
    is_complete: Callable[[SessionState], bool]
    next_stage: Optional[str] = None
    artifact_keys: List[str] = field(default_factory=list)


# ============================================================
#  Helpers
# ============================================================

def _has_min_facts(session: SessionState, min_count: int = 2, min_len: int = 4) -> bool:
    """考古素材至少 N 条,每条 ≥ M 字符"""
    facts = session.archaeology_facts or []
    if len(facts) < min_count:
        return False
    for f in facts:
        text = (f.get("text") if isinstance(f, dict) else str(f)) or ""
        if len(text.strip()) < min_len:
            return False
    return True


def _has_identity_selection(session: SessionState) -> bool:
    return bool(session.identity_selections)


def _has_music_spec(session: SessionState) -> bool:
    spec = session.music_spec or {}
    return bool(
        spec.get("bpm") is not None
        and spec.get("key")
        and spec.get("genre")
        and spec.get("instruments")
    )


def _has_lyrics(session: SessionState, min_len: int = 30) -> bool:
    p = session.current_proposal or {}
    lyrics = (p.get("lyrics") if p else "") or ""
    return len(lyrics.strip()) >= min_len


def _has_usable_highlight(session: SessionState) -> bool:
    if session.generation_status not in ("READY", "READY_WITH_FAILURES"):
        return False
    clips = session.highlight_clips or []
    return any(c.get("status") == "READY" for c in clips)


def _has_full_song_validated(session: SessionState) -> bool:
    h = session.selected_highlight or {}
    return bool(
        h.get("full_song_path")
        and h.get("full_song_sha256")
        and h.get("full_song_url")
    )


def _has_personal_style_profile(session: SessionState) -> bool:
    p = session.personal_style_profile or {}
    return bool(p.get("archaeology_facts")) and bool(p.get("music_spec"))


# ============================================================
#  7 个阶段
# ============================================================

# Stage 1: 声音考古
ARCHAEOLOGY = StagePolicy(
    stage="ARCHAEOLOGY",
    description="收集用户的真实声音素材(场景/情绪/记忆)",
    min_evidence_level=EvidenceLevel.STRONG,
    risk_if_incomplete=RiskLevel.LOW,
    artifact_keys=["archaeology_facts"],
    can_enter=lambda s: True,                # 任何 session 创建后即可进入
    is_complete=lambda s: _has_min_facts(s, min_count=2, min_len=4),
    next_stage="IDENTITY_FITTING",
)


# Stage 2: 身份试衣间
IDENTITY_FITTING = StagePolicy(
    stage="IDENTITY_FITTING",
    description="从 archaeology 中提炼 3 个 persona 候选,用户 keep/borrow/reject",
    min_evidence_level=EvidenceLevel.MEDIUM,
    risk_if_incomplete=RiskLevel.LOW,
    artifact_keys=["identity_candidates", "identity_selections"],
    can_enter=lambda s: ARCHAEOLOGY.is_complete(s),    # 必须先有考古
    is_complete=lambda s: _has_identity_selection(s),
    next_stage="VARIABLE_SETUP",
)


# Stage 3: 变量工作台
VARIABLE_SETUP = StagePolicy(
    stage="VARIABLE_SETUP",
    description="设定 bpm / key / genre / instruments",
    min_evidence_level=EvidenceLevel.MEDIUM,
    risk_if_incomplete=RiskLevel.LOW,
    artifact_keys=["music_spec"],
    can_enter=lambda s: IDENTITY_FITTING.is_complete(s),
    is_complete=lambda s: _has_music_spec(s),
    next_stage="PROPOSAL_REVIEW",
)


# Stage 4: 共同导演(歌词提案)
PROPOSAL_REVIEW = StagePolicy(
    stage="PROPOSAL_REVIEW",
    description="展示歌词 + 音乐规格提案,用户 confirm/edit/discuss",
    min_evidence_level=EvidenceLevel.STRONG,
    risk_if_incomplete=RiskLevel.MEDIUM,             # 歌词是核心交付物
    artifact_keys=["current_proposal"],
    can_enter=lambda s: VARIABLE_SETUP.is_complete(s),
    is_complete=lambda s: _has_lyrics(s, min_len=30) and bool(s.current_proposal),
    next_stage="HIGHLIGHT_REVIEW",
)


# Stage 5: 片段评估
HIGHLIGHT_REVIEW = StagePolicy(
    stage="HIGHLIGHT_REVIEW",
    description="生成 3 个候选 30s 高亮,用户选 1 个",
    min_evidence_level="tool",                      # 必须有工具产物
    risk_if_incomplete=RiskLevel.HIGH,               # 不可逆:已经消耗音乐模型
    artifact_keys=["highlight_clips", "selected_highlight"],
    can_enter=lambda s: PROPOSAL_REVIEW.is_complete(s),
    is_complete=lambda s: bool(s.selected_highlight),
    next_stage="FULL_REVIEW",
)


# Stage 6: 完整作品
FULL_REVIEW = StagePolicy(
    stage="FULL_REVIEW",
    description="从选中片段生成完整歌曲,等用户确认",
    min_evidence_level="tool",
    risk_if_incomplete=RiskLevel.MEDIUM,
    artifact_keys=["selected_highlight.full_song_*"],
    can_enter=lambda s: HIGHLIGHT_REVIEW.is_complete(s),
    is_complete=lambda s: _has_full_song_validated(s),
    next_stage="ARCHIVE",
)


# Stage 7: 记忆沉淀
ARCHIVE = StagePolicy(
    stage="ARCHIVE",
    description="把完整的 personal_style_profile 写入长期档案(只有用户 confirm 才写)",
    min_evidence_level=EvidenceLevel.STRONG,
    risk_if_incomplete=RiskLevel.HIGH,               # 不可逆:影响长期记忆
    artifact_keys=["personal_style_profile"],
    can_enter=lambda s: FULL_REVIEW.is_complete(s),
    is_complete=lambda s: _has_personal_style_profile(s),
    next_stage=None,                                  # 终点
)


# ============================================================
#  阶段注册表
# ============================================================

ALL_STAGES = [
    ARCHAEOLOGY,
    IDENTITY_FITTING,
    VARIABLE_SETUP,
    PROPOSAL_REVIEW,
    HIGHLIGHT_REVIEW,
    FULL_REVIEW,
    ARCHIVE,
]

STAGE_BY_NAME = {s.stage: s for s in ALL_STAGES}

# 顺序:作为阶段推进的默认路径
STAGE_ORDER = [s.stage for s in ALL_STAGES]


def get_stage(name: str) -> Optional[StagePolicy]:
    if name not in STAGE_BY_NAME:
        return None
    return STAGE_BY_NAME[name]


def next_stage_of(name: str) -> Optional[str]:
    p = get_stage(name)
    if p is None:
        return None
    return p.next_stage


# ============================================================
#  Validation helpers
# ============================================================

@dataclass
class StageValidation:
    stage: str
    can_enter: bool
    is_complete: bool
    missing_requirements: List[str] = field(default_factory=list)
    risk_if_proceed: str = RiskLevel.LOW


def validate_stage_transition(session: SessionState, target_stage: str) -> StageValidation:
    """检查能否从 session.current_stage 推到 target_stage

    返回结构化结果,Agent Core 据此决策 continue / escalate。
    """
    target = get_stage(target_stage)
    if target is None:
        return StageValidation(
            stage=target_stage,
            can_enter=False,
            is_complete=False,
            missing_requirements=[f"unknown_stage:{target_stage}"],
            risk_if_proceed=RiskLevel.LOW,
        )
    can_enter = target.can_enter(session)
    is_complete = target.is_complete(session)

    missing = []
    if not can_enter:
        missing.append(f"entry_requirements_unmet_for:{target_stage}")
    if not is_complete:
        missing.append(f"completion_unmet_for:{target_stage}")

    return StageValidation(
        stage=target_stage,
        can_enter=can_enter,
        is_complete=is_complete,
        missing_requirements=missing,
        risk_if_proceed=target.risk_if_incomplete,
    )
