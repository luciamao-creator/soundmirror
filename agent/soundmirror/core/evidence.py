"""Evidence Assessor — 证据等级评估

从 session + input + tool artifacts 三个维度评估每个关键字段的证据强度。
输出可检查的 Evidence 列表,Agent Core 据此决策 continue / ask / escalate。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .types import (
    SessionState,
    Evidence,
    EvidenceLevel,
    EvidenceSource,
)


# ============================================================
#  Evidence Field Registry
# ============================================================

@dataclass
class EvidenceFieldSpec:
    """一个字段的证据要求"""
    field: str                                  # "archaeology_facts" / "music_spec.bpm"
    min_level: str                              # 最低允许等级
    required: bool = True                       # 是否必需
    rationale: str = ""                         # 为什么需要这个等级


@dataclass
class EvidenceAssessment:
    fields: Dict[str, Evidence] = field(default_factory=dict)
    weakest_field: Optional[str] = None
    weakest_level: str = EvidenceLevel.STRONG
    below_threshold: List[str] = field(default_factory=list)

    def is_sufficient(self) -> bool:
        return not self.below_threshold


def _level_rank(level: str) -> int:
    return {
        EvidenceLevel.STRONG: 4,
        EvidenceLevel.MEDIUM: 3,
        EvidenceLevel.WEAK: 2,
        EvidenceLevel.ABSENT: 1,
    }.get(level, 0)


def _min_meets(actual: str, required: str) -> bool:
    return _level_rank(actual) >= _level_rank(required)


# ============================================================
#  Field-by-field assessment
# ============================================================

def assess_archaeology_facts(session: SessionState) -> Evidence:
    facts = session.archaeology_facts or []
    if not facts:
        return Evidence(
            field="archaeology_facts",
            source=EvidenceSource.DEFAULT,
            level=EvidenceLevel.ABSENT,
            text=None,
            confidence=0.0,
        )

    # 至少 2 条 + 每条 ≥ 4 字符 → strong
    has_min_count = len(facts) >= 2
    has_min_length = all(
        len((f.get("text", "") if isinstance(f, dict) else str(f)).strip()) >= 4
        for f in facts
    )
    if has_min_count and has_min_length:
        level = EvidenceLevel.STRONG
        confidence = 1.0
    elif has_min_count or has_min_length:
        level = EvidenceLevel.MEDIUM
        confidence = 0.7
    else:
        level = EvidenceLevel.WEAK
        confidence = 0.4

    return Evidence(
        field="archaeology_facts",
        source=EvidenceSource.USER_EXPLICIT,
        level=level,
        text=str([f.get("text", "") if isinstance(f, dict) else str(f) for f in facts])[:200],
        confidence=confidence,
    )


def assess_identity(session: SessionState) -> Evidence:
    selections = session.identity_selections or []
    if not selections:
        return Evidence(
            field="identity_selections",
            source=EvidenceSource.DEFAULT,
            level=EvidenceLevel.ABSENT,
            confidence=0.0,
        )
    return Evidence(
        field="identity_selections",
        source=EvidenceSource.USER_EXPLICIT,
        level=EvidenceLevel.STRONG,
        text=str(selections)[:200],
        confidence=1.0,
    )


def assess_music_spec(session: SessionState) -> Evidence:
    spec = session.music_spec or {}
    if not spec:
        return Evidence(
            field="music_spec",
            source=EvidenceSource.DEFAULT,
            level=EvidenceLevel.ABSENT,
            confidence=0.0,
        )
    has_all = all([
        spec.get("bpm") is not None,
        spec.get("key"),
        spec.get("genre"),
        spec.get("instruments"),
    ])
    if has_all:
        return Evidence(
            field="music_spec",
            source=EvidenceSource.USER_EXPLICIT,
            level=EvidenceLevel.STRONG,
            text=str(spec)[:200],
            confidence=1.0,
        )
    return Evidence(
        field="music_spec",
        source=EvidenceSource.USER_IMPLIED,
        level=EvidenceLevel.WEAK,
        text=f"partial spec: {spec}",
        confidence=0.5,
    )


def assess_lyrics(session: SessionState) -> Evidence:
    p = session.current_proposal or {}
    lyrics = (p.get("lyrics") if p else "") or ""
    if not lyrics:
        return Evidence(
            field="current_proposal.lyrics",
            source=EvidenceSource.DEFAULT,
            level=EvidenceLevel.ABSENT,
            confidence=0.0,
        )
    if len(lyrics.strip()) < 30:
        return Evidence(
            field="current_proposal.lyrics",
            source=EvidenceSource.INFERRED,
            level=EvidenceLevel.WEAK,
            text=lyrics[:200],
            confidence=0.3,
        )
    return Evidence(
        field="current_proposal.lyrics",
        source=EvidenceSource.MUTATION,
        level=EvidenceLevel.STRONG,
        text=lyrics[:200],
        artifact=f"proposal:{session.cs_id}",
        confidence=0.95,
    )


def assess_highlight_clips(session: SessionState) -> Evidence:
    clips = session.highlight_clips or []
    if not clips:
        return Evidence(
            field="highlight_clips",
            source=EvidenceSource.DEFAULT,
            level=EvidenceLevel.ABSENT,
            confidence=0.0,
        )
    ready = [c for c in clips if c.get("status") == "READY"]
    if not ready:
        return Evidence(
            field="highlight_clips",
            source=EvidenceSource.TOOL,
            level=EvidenceLevel.WEAK,
            text=f"{len(clips)} clips but none READY (gen_status={session.generation_status})",
            confidence=0.3,
        )
    # 有 1+ READY clip + checksum → strong
    has_checksum = all(c.get("checksum_sha256") for c in ready)
    return Evidence(
        field="highlight_clips",
        source=EvidenceSource.TOOL,
        level=EvidenceLevel.STRONG if has_checksum else EvidenceLevel.MEDIUM,
        text=f"{len(ready)}/{len(clips)} READY",
        artifact=f"highlight:{session.cs_id}",
        confidence=1.0 if has_checksum else 0.8,
    )


def assess_selected_highlight(session: SessionState) -> Evidence:
    h = session.selected_highlight or {}
    if not h:
        return Evidence(
            field="selected_highlight",
            source=EvidenceSource.DEFAULT,
            level=EvidenceLevel.ABSENT,
            confidence=0.0,
        )
    return Evidence(
        field="selected_highlight",
        source=EvidenceSource.USER_EXPLICIT,
        level=EvidenceLevel.STRONG,
        text=str(h)[:200],
        artifact=f"highlight_choice:{h.get('slot', '?')}",
        confidence=1.0,
    )


def assess_full_song(session: SessionState) -> Evidence:
    h = session.selected_highlight or {}
    path = h.get("full_song_path")
    sha = h.get("full_song_sha256")
    if not path or not sha:
        return Evidence(
            field="selected_highlight.full_song",
            source=EvidenceSource.DEFAULT,
            level=EvidenceLevel.ABSENT,
            confidence=0.0,
        )
    return Evidence(
        field="selected_highlight.full_song",
        source=EvidenceSource.TOOL,
        level=EvidenceLevel.STRONG,
        text=f"path={path} sha256={sha[:16]}...",
        artifact=path,
        confidence=1.0,
    )


def assess_personal_style_profile(session: SessionState) -> Evidence:
    p = session.personal_style_profile or {}
    if not p.get("archaeology_facts") or not p.get("music_spec"):
        return Evidence(
            field="personal_style_profile",
            source=EvidenceSource.DEFAULT,
            level=EvidenceLevel.ABSENT,
            confidence=0.0,
        )
    # 必须用户 confirm 后才 strong;否则 weak
    return Evidence(
        field="personal_style_profile",
        source=EvidenceSource.MUTATION,
        level=EvidenceLevel.STRONG,
        text=f"profile written at {p.get('archived_at', '?')}",
        artifact=f"profile:{session.cs_id}",
        confidence=1.0,
    )


# ============================================================
#  Per-field specs
# ============================================================

EVIDENCE_SPECS: Dict[str, EvidenceFieldSpec] = {
    "archaeology_facts": EvidenceFieldSpec(
        field="archaeology_facts",
        min_level=EvidenceLevel.STRONG,
        rationale="歌词由 archaeology 拼出,素材不足会写出空洞词",
    ),
    "identity_selections": EvidenceFieldSpec(
        field="identity_selections",
        min_level=EvidenceLevel.MEDIUM,
        rationale="persona 决定 prompt 风格,未选会让生成结果不可控",
    ),
    "music_spec": EvidenceFieldSpec(
        field="music_spec",
        min_level=EvidenceLevel.MEDIUM,
        rationale="BPM/key/genre/instruments 缺失时,模型按默认输出",
    ),
    "current_proposal.lyrics": EvidenceFieldSpec(
        field="current_proposal.lyrics",
        min_level=EvidenceLevel.STRONG,
        rationale="歌词是核心交付物,不可猜测",
    ),
    "highlight_clips": EvidenceFieldSpec(
        field="highlight_clips",
        min_level="tool",
        rationale="必须有 tool 产物,用户必须有可听的产物",
    ),
    "selected_highlight": EvidenceFieldSpec(
        field="selected_highlight",
        min_level=EvidenceLevel.STRONG,
        rationale="用户必须明示选了哪个,Agent 不能猜",
    ),
    "selected_highlight.full_song": EvidenceFieldSpec(
        field="selected_highlight.full_song",
        min_level="tool",
        rationale="完整歌曲必须有文件 + sha256 + url",
    ),
    "personal_style_profile": EvidenceFieldSpec(
        field="personal_style_profile",
        min_level=EvidenceLevel.STRONG,
        rationale="归档影响长期风格记忆,必须 confirm",
    ),
}


# ============================================================
#  Main entry
# ============================================================

ASSESSORS = {
    "archaeology_facts": assess_archaeology_facts,
    "identity_selections": assess_identity,
    "music_spec": assess_music_spec,
    "current_proposal.lyrics": assess_lyrics,
    "highlight_clips": assess_highlight_clips,
    "selected_highlight": assess_selected_highlight,
    "selected_highlight.full_song": assess_full_song,
    "personal_style_profile": assess_personal_style_profile,
}


def assess_session(session: SessionState, fields: Optional[List[str]] = None) -> EvidenceAssessment:
    """评估 session 各字段证据等级

    Args:
        session: 当前 session
        fields: 要评估的字段;None 时评估所有
    """
    fields = fields or list(ASSESSORS.keys())
    out: Dict[str, Evidence] = {}

    for f in fields:
        if f in ASSESSORS:
            out[f] = ASSESSORS[f](session)

    # 找最弱
    weakest_field = None
    weakest_level = EvidenceLevel.STRONG
    for f, ev in out.items():
        if _level_rank(ev.level) < _level_rank(weakest_level):
            weakest_level = ev.level
            weakest_field = f

    # 找低于 spec 的字段
    below = []
    for f, ev in out.items():
        spec = EVIDENCE_SPECS.get(f)
        if not spec:
            continue
        if spec.required and not _min_meets(ev.level, spec.min_level):
            below.append(f)

    return EvidenceAssessment(
        fields=out,
        weakest_field=weakest_field,
        weakest_level=weakest_level,
        below_threshold=below,
    )


def field_evidence_summary(session: SessionState) -> str:
    """人类可读的证据摘要"""
    assessment = assess_session(session)
    lines = [f"  {f}: {ev.level} ({ev.source}, conf={ev.confidence:.2f})"
             for f, ev in assessment.fields.items()]
    return "\n".join(lines) if lines else "  (no fields assessed)"
