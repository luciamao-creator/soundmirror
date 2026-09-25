"""Agent Core 数据契约

设计原则:
  - 全部 dataclass,可 JSON 序列化,便于 trace 和评测
  - 字段命名 snake_case
  - 枚举值用 str,前端可读
"""
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ============================================================
#  决策
# ============================================================

class DecisionKind:
    CONTINUE = "continue"      # 推进(可能驱动 mutation / tool call)
    ASK = "ask"                # 向人询问(明确 missing_fields + ask_options)
    CONFIRM = "confirm"        # 高影响动作需用户二次确认(明确 consequence)
    RETRY = "retry"            # 重试(明确 retry_target + retry_after)
    STOP = "stop"              # 终止(明确 stop_reason)
    ESCALATE = "escalate"      # 升级(明确 escalate_to + escalate_reason)


# ============================================================
#  证据(Evidence)
# ============================================================

class EvidenceLevel:
    STRONG = "strong"          # 用户明确表述、有物证
    MEDIUM = "medium"          # 用户隐含表达、有上下文支撑
    WEAK = "weak"              # 推断/默认,无直接证据
    ABSENT = "absent"          # 完全没证据


class EvidenceSource:
    USER_EXPLICIT = "user_explicit"        # 用户明示("我想要 X")
    USER_IMPLIED = "user_implied"          # 用户暗示(从上下文推断但有支持)
    INFERRED = "inferred"                  # Agent 推断
    TOOL = "tool"                          # 工具产物(如音乐生成的文件)
    DEFAULT = "default"                    # 系统默认
    MUTATION = "mutation"                  # 之前的 stage mutation 派生


@dataclass
class Evidence:
    field: str                                # e.g. "archaeology.anchor_time"
    source: str                               # EvidenceSource.*
    level: str                                # EvidenceLevel.*
    text: Optional[str] = None                # 原始文本证据
    artifact: Optional[str] = None            # 物证(file path / node_id)
    confidence: float = 1.0                   # 0-1

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
#  风险(Risk)
# ============================================================

class RiskLevel:
    LOW = "low"            # 无害或可恢复
    MEDIUM = "medium"      # 影响中等,需要明确告知
    HIGH = "high"          # 不可逆 / 高影响,必须确认


@dataclass
class RiskSignal:
    field: str                                # e.g. "voice.persona_clone"
    level: str                                # RiskLevel.*
    rationale: str                            # 为什么是这个等级
    mitigation: Optional[str] = None          # 缓解措施

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
#  询问(Ask)
# ============================================================

@dataclass
class AskOption:
    """给用户的选项"""
    value: str                                # 选项值(用作 user response 匹配)
    label: str                                # 展示文字
    description: Optional[str] = None         # 详细说明
    is_default: bool = False                  # 是否默认值(供 null response 使用)


@dataclass
class AskRule:
    """什么时候问什么。可检查的规则,不是 prompt。"""
    rule_id: str                              # e.g. "ask.persona.preferred_timbre"
    field: str                                # 询问针对的字段
    why: str                                  # 缺这个字段会怎样(写到 evidence 风险说明)
    options: List[AskOption]                  # 至少 2 个选项
    when_missing: Optional[str] = None        # 触发条件:从 session 状态判断
    priority: int = 0                         # 多 ask 并存时的优先级(高先问)


# ============================================================
#  工具调用(Tool Call)
# ============================================================

@dataclass
class ToolCall:
    tool_id: str                              # "music.generate" / "music.generate_full"
    arguments: Dict[str, Any]
    result: Optional[Dict[str, Any]] = None   # 工具返回值(成功时填)
    error: Optional[str] = None               # 错误信息(失败时填)
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
#  输入事件
# ============================================================

class InputKind:
    TEXT = "text"                              # 用户文本消息
    ACTION = "action"                          # 用户动作(button click / command)
    TOOL_CALLBACK = "tool_callback"            # 工具完成回调
    RESUME = "resume"                          # 系统恢复信号


@dataclass
class InputEvent:
    """输入事件(由 InputAdapter 产生)"""
    event_id: str                              # 全局唯一
    kind: str                                  # InputKind.*
    actor_user_id: str
    chat_id: str
    text: str = ""
    action_id: Optional[str] = None
    action_payload: Optional[dict] = None
    tool_callback: Optional[dict] = None       # TOOL_CALLBACK 时填
    continue_yes_no: Optional[bool] = None     # confirm 的回应
    confirm_risk: Optional[bool] = None        # 高风险动作的确认
    selected_option: Optional[str] = None      # ask 的回应
    occurred_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
#  Session / Stage
# ============================================================

@dataclass
class StageSnapshot:
    """某个阶段在某一时刻的状态"""
    stage: str
    entered_at: float = 0.0
    completed_at: Optional[float] = None
    artifacts: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionState:
    """Agent 视角的 session 状态(从 state_store 投影)"""
    cs_id: str
    product_user_id: str
    chat_id: str
    current_stage: str
    state_revision: int
    archaeology_facts: List[dict] = field(default_factory=list)
    identity_selections: List[dict] = field(default_factory=list)
    identity_candidates: List[dict] = field(default_factory=list)
    music_spec: Dict[str, Any] = field(default_factory=dict)
    current_proposal: Optional[Dict[str, Any]] = None
    highlight_clips: List[dict] = field(default_factory=list)
    generation_status: Optional[str] = None
    selected_highlight: Optional[Dict[str, Any]] = None
    full_song: Optional[Dict[str, Any]] = None
    personal_style_profile: Optional[Dict[str, Any]] = None
    generation_job_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
#  Trace
# ============================================================

@dataclass
class TraceStep:
    """Agent 一次 handle() 的完整轨迹

    关键:
      - input: 输入什么
      - signals: 信号抽取结果
      - evidence: 证据评估
      - risk: 风险评估
      - decision: 最终决策
      - tool_calls: 工具调用
      - state_diff: 状态变化前/后
      - validation: 验证结果(完成条件检查等)
    """
    trace_id: str
    session_id: str
    step_index: int
    started_at: float
    finished_at: float = 0.0
    input: Optional[InputEvent] = None
    stage_before: str = ""
    stage_after: str = ""
    revision_before: int = 0
    revision_after: int = 0
    decision: Optional["AgentDecision"] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)
    risk_signals: List[RiskSignal] = field(default_factory=list)
    validation: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        # 处理 dataclass 嵌套
        if self.input is not None:
            d["input"] = self.input.to_dict()
        if self.decision is not None:
            d["decision"] = self.decision.to_dict()
        return d


# ============================================================
#  AgentDecision(主决策)
# ============================================================

@dataclass
class AgentDecision:
    """Agent Core 的主决策

    kind 决定主流程:
      - continue: 推进(可能调 mutation / tool),不需用户介入
      - ask: 必须等用户回应(ask_options)
      - confirm: 必须等用户二次确认(高影响动作)
      - retry: 重试上次失败的 tool
      - stop: 终止
      - escalate: 升级(超出 Agent 能力)
    """
    kind: str                                       # DecisionKind.*
    reasoning: str                                  # 决策原因(人类可读)
    evidence: List[Evidence] = field(default_factory=list)
    risk_signals: List[RiskSignal] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)        # ask
    ask_options: List[AskOption] = field(default_factory=list)     # ask
    risk_level: str = RiskLevel.LOW                                # confirm 用
    consequence: Optional[str] = None                             # confirm 用
    confidence: float = 1.0                                       # 0-1
    stage: str = "INIT"
    next_stage: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    state_revision_before: int = 0
    state_revision_after: int = 0
    trace_id: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def is_terminal(self) -> bool:
        return self.kind in (DecisionKind.STOP, DecisionKind.ESCALATE)

    def requires_user_response(self) -> bool:
        return self.kind in (DecisionKind.ASK, DecisionKind.CONFIRM)


# ============================================================
#  Stage Status
# ============================================================

class StageStatus:
    """阶段状态机"""
    NOT_ENTERED = "not_entered"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"                            # 证据不足 / 风险高 / 缺字段
