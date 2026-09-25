"""SoundMirror Agent Core v0

不依赖飞书、不依赖公网、不依赖 Matrix/MiniMax Music/YuE2。

核心模块:
  types            数据契约(AgentDecision / Evidence / RiskSignal / AskRule / TraceStep)
  stage_policy     7 个阶段的进入/完成条件
  ask_rules        询问策略(可检查规则,不是 prompt)
  evidence         证据等级评估
  risk             风险等级评估
  signal_extractor 输入信号解析
  tool_adapter     Tool 接口(音乐 / Lark / 资源上传 / 音频剪辑)
  music_tool_mock  音乐生成 mock 实现
  lark_tool_mock   飞书发送 mock 实现
  agent_core       主循环
  recovery         中断恢复
  trace            运行轨迹
  deprecated       一次性 import 所有 deprecated 模块,标 __deprecated__
"""
from .types import (
    AgentDecision,
    DecisionKind,
    Evidence,
    EvidenceLevel,
    EvidenceSource,
    RiskSignal,
    RiskLevel,
    AskRule,
    AskOption,
    ToolCall,
    TraceStep,
    InputEvent,
    InputKind,
    SessionState,
    StageSnapshot,
)
from .stage_policy import (
    ALL_STAGES,
    ARCHAEOLOGY,
    IDENTITY_FITTING,
    VARIABLE_SETUP,
    PROPOSAL_REVIEW,
    HIGHLIGHT_REVIEW,
    FULL_REVIEW,
    ARCHIVE,
    STAGE_ORDER,
    STAGE_BY_NAME,
    StagePolicy,
    StageValidation,
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
    EvidenceFieldSpec,
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
    SendCardResult,
    time_call,
    ensure_dir,
)
from .music_tool_mock import (
    MockMusicAdapter,
    MusicToolAdapter as MusicToolAdapterWrapped,
    GenerateCandidatesRequest,
    GenerateCandidatesResult,
    CandidateResult,
    make_default_music_tool,
)
from .lark_tool_mock import (
    MockLarkAdapter,
    make_default_lark_tool,
)
from .agent_core import (
    AgentCore,
    project_session,
)
from .recovery import (
    RecoveryPlan,
    detect_in_flight,
    verify_clips_on_disk,
)
from .trace import (
    TraceRecorder,
    InMemoryTraceRecorder,
)

__version__ = "0.1.0"

