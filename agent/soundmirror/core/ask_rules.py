"""Ask Rules — 询问策略(可检查规则,不是 prompt)

设计:每条规则都是一个 dataclass,描述"什么时候问什么、为什么问、给什么选项"。
Agent Core 在每个 stage 评估哪些规则命中,产出 AskDecision。
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .types import (
    SessionState,
    AskRule,
    AskOption,
    EvidenceLevel,
)


# ============================================================
#  Built-in Ask Rules
# ============================================================

# 1. 询问"声音档案第 1 段素材" — 当 archaeology 空时
ASK_ARCHAEOLOGY_FIRST = AskRule(
    rule_id="ask.archaeology.first_fact",
    field="archaeology_facts",
    why="没有素材就无法生成歌词;用户说一句话即可,真实场景比抽象风格描述更准",
    priority=100,
    options=[
        AskOption(
            value="引导",
            label="我不确定从哪开始",
            description="请 Agent 引导(询问场景/时间/情绪)",
            is_default=True,
        ),
        AskOption(
            value="例子",
            label="看几个例子",
            description="Agent 提供 2-3 个示例事实,用户挑一个或改写",
        ),
    ],
)


# 2. 询问"声音档案补充" — 当 archaeology 已 1 条但 < 2 时
ASK_ARCHAEOLOGY_SECOND = AskRule(
    rule_id="ask.archaeology.second_fact",
    field="archaeology_facts",
    why="至少 2 个不同维度的素材能产出更有层次的歌词(场景 + 情绪 / 记忆 + 感官)",
    priority=90,
    options=[
        AskOption(
            value="另一个时间",
            label="我想再加一个不同时段",
            description="比如第一次素材是清晨,这次可以是傍晚/深夜",
        ),
        AskOption(
            value="另一种感官",
            label="我想换一个感官维度",
            description="比如第一次是画面,这次可以是声音/气味/触感",
        ),
        AskOption(
            value="够了",
            label="够了,进入下一步",
            description="跳到 IDENTITY_FITTING",
            is_default=True,
        ),
    ],
)


# 3. 询问 persona 偏好 — 当 user 不愿 keep 任何一个候选
ASK_PERSONA_DISLIKE = AskRule(
    rule_id="ask.persona.dislike",
    field="identity_candidates",
    why="如果 3 个候选都不喜欢,继续推进会强行匹配,生成用户不满意的音乐",
    priority=80,
    options=[
        AskOption(
            value="borrow",
            label="借一个再改",
            description="Agent 保留一个候选并根据反馈调整",
        ),
        AskOption(
            value="regenerate",
            label="重新生成 3 个候选",
            description="Agent 用更具体的考古素材重新抽取",
        ),
        AskOption(
            value="skip",
            label="跳过 persona,直接进变量",
            description="使用默认 persona 配置",
        ),
    ],
)


# 4. 询问音乐风格细节 — 当 user 给的 spec 模糊
ASK_MUSIC_STYLE_CLARIFY = AskRule(
    rule_id="ask.music.style_clarify",
    field="music_spec",
    why="风格描述模糊(如 '清新') 会导致生成不稳定;明确 BPM/调性/参考曲目可大幅提高准确度",
    priority=70,
    options=[
        AskOption(
            value="用默认",
            label="用平台默认 indie 配置",
            description="BPM 88 / C key / indie / piano + ambient pad",
            is_default=True,
        ),
        AskOption(
            value="我给参考",
            label="我给一首参考曲",
            description="User 输入'参考 X 曲的 Y 段落',Agent 推断 spec",
        ),
        AskOption(
            value="自己调",
            label="我自己改 BPM/key/乐器",
            description="User 主动改字段",
        ),
    ],
)


# 5. 询问歌词改写方向 — 当 user 说"歌词要改"
ASK_LYRIC_EDIT_DIRECTION = AskRule(
    rule_id="ask.lyric.edit_direction",
    field="current_proposal.lyrics",
    why="改写需求模糊会导致 Agent 反复猜测;明确方向(改词/改结构/改情绪)可一次到位",
    priority=85,
    options=[
        AskOption(
            value="改词",
            label="只改某些词",
            description="保留结构,改具体字词",
        ),
        AskOption(
            value="改结构",
            label="改 verse/chorus 结构",
            description="比如加一段 bridge,或减一段",
        ),
        AskOption(
            value="改情绪",
            label="保留大意,换情绪色彩",
            description="比如从忧郁变平静",
        ),
    ],
)


# 6. 询问音乐生成失败 — 当 tool 失败
ASK_GENERATION_FAILURE = AskRule(
    rule_id="ask.generation.failure",
    field="highlight_clips",
    why="音乐生成失败后,用户需要明确下一步,而不是 Agent 自行重试或放弃",
    priority=95,
    options=[
        AskOption(
            value="retry_same",
            label="用同一 spec 重试",
            description="可能是网络抖动,重试一次",
        ),
        AskOption(
            value="adjust_spec",
            label="先调整 spec 再重试",
            description="降 BPM / 换乐器 / 简化 prompt",
        ),
        AskOption(
            value="abort",
            label="放弃,回到 PROPOSAL_REVIEW",
            description="保留 lyrics 但不生成,等下次",
        ),
    ],
)


# 7. 询问个人风格档案归档 — FULL_REVIEW 完成后
ASK_ARCHIVE_CONFIRM = AskRule(
    rule_id="ask.archive.confirm",
    field="personal_style_profile",
    why="归档是长期动作,影响个人风格记忆,必须用户确认;未经确认的内容不能进入稳定档案",
    priority=100,
    options=[
        AskOption(
            value="confirm",
            label="确认归档",
            description="personal_style_profile 写入稳定档案,未来可基于此生成",
        ),
        AskOption(
            value="revise",
            label="先改一下",
            description="告诉 Agent 哪些字段不对",
        ),
        AskOption(
            value="discard",
            label="这次不归档",
            description="完整歌曲保留,但不写入风格档案",
        ),
    ],
)


# 8. 询问高风险动作确认 — 见 risk.py
ASK_CONFIRM_RISKY = AskRule(
    rule_id="ask.confirm.always",
    field="*",
    why="Agent 决定执行高影响动作(消耗生成配额/写入长期档案/不可逆删除),必须用户显式确认",
    priority=200,
    options=[
        AskOption(
            value="confirm",
            label="确认",
        ),
        AskOption(
            value="cancel",
            label="取消",
            is_default=True,
        ),
    ],
)


# ============================================================
#  Rule registry
# ============================================================

ALL_ASK_RULES: List[AskRule] = [
    ASK_ARCHAEOLOGY_FIRST,
    ASK_ARCHAEOLOGY_SECOND,
    ASK_PERSONA_DISLIKE,
    ASK_MUSIC_STYLE_CLARIFY,
    ASK_LYRIC_EDIT_DIRECTION,
    ASK_GENERATION_FAILURE,
    ASK_ARCHIVE_CONFIRM,
    ASK_CONFIRM_RISKY,
]


# ============================================================
#  Ask Evaluator
# ============================================================

@dataclass
class AskEvaluation:
    """Ask 评估结果:命中哪些规则"""
    triggered_rules: List[AskRule] = field(default_factory=list)
    pending_field: Optional[str] = None
    ask_options: List[AskOption] = field(default_factory=list)
    reason: str = ""

    def should_ask(self) -> bool:
        return bool(self.triggered_rules)

    def top_rule(self) -> Optional[AskRule]:
        if not self.triggered_rules:
            return None
        return max(self.triggered_rules, key=lambda r: r.priority)


def evaluate_asks(
    session: SessionState,
    current_stage: str,
    triggered_inputs: List[str] = None,        # 用户最近的输入信号
    tool_failures: List[str] = None,           # 工具失败信号
    high_risk: bool = False,                   # 是否有高风险动作
) -> AskEvaluation:
    """根据 session 状态 + 输入信号评估应触发哪些 Ask 规则

    Args:
        session: 当前 session
        current_stage: 当前阶段
        triggered_inputs: 最近用户输入的关键词,如 "改歌词"
        tool_failures: 工具失败列表,如 ["music.generate"]
        high_risk: 是否有高影响动作需确认
    """
    triggered = []
    triggered_inputs = triggered_inputs or []
    tool_failures = tool_failures or []

    # 1. ARCHAEOLOGY_FIRST: stage=ARCHAEOLOGY 且 facts=空
    if current_stage == "ARCHAEOLOGY" and not session.archaeology_facts:
        triggered.append(ASK_ARCHAEOLOGY_FIRST)

    # 2. ARCHAEOLOGY_SECOND: facts 数量 < 2
    elif current_stage == "ARCHAEOLOGY" and len(session.archaeology_facts or []) < 2:
        triggered.append(ASK_ARCHAEOLOGY_SECOND)

    # 3. PERSONA_DISLIKE: IDENTITY_FITTING 且用户最近 reject/borrow
    if current_stage == "IDENTITY_FITTING":
        for action in (session.identity_selections or []):
            if action.get("action") in ("reject", "borrow"):
                triggered.append(ASK_PERSONA_DISLIKE)
                break

    # 4. MUSIC_STYLE_CLARIFY: VARIABLE_SETUP 且 spec 不完整且最近输入模糊
    if current_stage == "VARIABLE_SETUP":
        spec = session.music_spec or {}
        if not spec:
            triggered.append(ASK_MUSIC_STYLE_CLARIFY)

    # 5. LYRIC_EDIT_DIRECTION: PROPOSAL_REVIEW 且用户最近输入含改写信号
    if current_stage == "PROPOSAL_REVIEW":
        edit_keywords = ["改", "换", "不行", "调整", "修改", "rewrite"]
        if any(kw in t for t in triggered_inputs for kw in edit_keywords):
            triggered.append(ASK_LYRIC_EDIT_DIRECTION)

    # 6. GENERATION_FAILURE: tool_failures 含 music.generate*
    if "music.generate" in tool_failures or "music.generate_full" in tool_failures:
        triggered.append(ASK_GENERATION_FAILURE)

    # 7. ARCHIVE_CONFIRM: ARCHIVE 阶段
    if current_stage == "ARCHIVE":
        triggered.append(ASK_ARCHIVE_CONFIRM)

    # 8. 高风险确认
    if high_risk:
        triggered.append(ASK_CONFIRM_RISKY)

    # 去重 + 按 priority 降序
    seen = set()
    unique = []
    for r in triggered:
        if r.rule_id not in seen:
            seen.add(r.rule_id)
            unique.append(r)
    unique.sort(key=lambda r: r.priority, reverse=True)

    top = unique[0] if unique else None
    return AskEvaluation(
        triggered_rules=unique,
        pending_field=top.field if top else None,
        ask_options=top.options if top else [],
        reason=f"{len(unique)} rules triggered" if unique else "no ask needed",
    )
