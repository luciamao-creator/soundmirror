# SoundMirror Agent Core v0.1.0 — 收口交付

> 日期:2026-09-30
> 状态:**收口完成,21/21 评测 PASS,老 213 测试不挂**

## 0. TL;DR

暂停飞书 / 公网 / 外部音乐模型后,Agent Core v0 落地:

```
不连飞书 / 不连 Matrix / 不连 MiniMax Music / 不连 YuE2
                    ↓
         Agent Core (orchestrator + stage policy + tools)
                    ↓
   CLI / Scripted input   →   主循环   →   mock tools
                    ↓
       完整 7 阶段流程可跑、可中断恢复、可评估
```

## 1. 交付清单 vs 用户要求

| 用户要求 | 状态 | 落地 |
|----------|------|------|
| 1. 盘点现有代码,分三类 | ✓ | `reports/V0_CODE_INVENTORY_2026-09-30.md` |
| 2. AgentDecision 数据结构(6 kind) | ✓ | `core/types.py::AgentDecision(kind=continue/ask/confirm/retry/stop/escalate)` |
| 3. 7 阶段进入 + 完成条件 | ✓ | `core/stage_policy.py`(ARCHAEOLOGY / IDENTITY_FITTING / VARIABLE_SETUP / PROPOSAL_REVIEW / HIGHLIGHT_REVIEW / FULL_REVIEW / ARCHIVE) |
| 4. 询问/证据/风险抽成可检查规则 | ✓ | `core/ask_rules.py`(8 rule) + `core/evidence.py`(8 assessors) + `core/risk.py`(forbidden/PII/状态一致性) |
| 5. 持久化任务状态 + 幂等 + 中断恢复 | ✓ | `state_store`(复用)+ `core/recovery.py::detect_in_flight` |
| 6. 音乐生成封装为独立 Tool Adapter | ✓ | `core/tool_adapter.py::MusicToolAdapter(Protocol)` + `core/music_tool_mock.py` |
| 7. 固定评测集 | ✓ | `core/eval_v0/test_core_smoke.py` — 21 用例,5 类(黄金/边界/负向/恢复/回归)+ 7 验收 |
| 8. 完整运行轨迹 | ✓ | `core/trace.py::TraceRecorder`(持久化到 SQLite trace 表) |

## 2. 验收标准 vs 实现

| 验收项 | 实现 | 测试 |
|--------|------|------|
| 不连接飞书也能完成完整创作流程 | Agent Core 不依赖 lark 模块,只走 mock | `test_acceptance_no_lark_dependency` PASS |
| 进程中断后能从最近一次安全状态恢复 | state_store 持久化 + recovery.detect_in_flight | `test_recovery_resume_after_kill` PASS |
| 信息充分时不会无意义追问 | ask_eval + signal_extractor(intent)联合判断 | `test_acceptance_no_useless_ask` PASS (0 ask in happy path) |
| 高影响歧义不会擅自推进 | risk.requires_confirm + confirm_risk 字段 | `test_acceptance_high_risk_no_unauthorized` PASS |
| 工具失败不会被误判为成功 | ToolCall.error 字段 + gen_status 真实反映 | `test_acceptance_tool_failure_detected` PASS |
| 未满足阶段完成条件时无法进入下一阶段 | stage_policy.is_complete + agent_core._advance_stage | `test_acceptance_no_stage_skip` PASS |
| 未经用户确认的推断不会写入长期稳定档案 | profile.archive 默认 risk=high,需 confirm_risk=True | `test_acceptance_no_archive_without_confirm` PASS |
| 后续更换飞书时,Agent Core 不需重写 | LarkToolAdapter Protocol + LarkInput 可后续实现 | (设计层面) |
| 后续更换音乐模型时,Agent Core 不需重写 | MusicToolAdapter Protocol + Mock 现存 | (设计层面) |

## 3. 评测结果

### 3.1 Agent Core v0 自有评测

```
================================================================================
  SoundMirror Agent Core v0 — 评测
================================================================================
  ✓ [golden     ] happy_path                          | all 7 stages passed
  ✓ [boundary   ] empty                               | handled without crash
  ✓ [boundary   ] short                               | handled without crash
  ✓ [boundary   ] long                                | handled without crash
  ✓ [boundary   ] emoji                               | handled without crash
  ✓ [boundary   ] mixed_lang                          | handled without crash
  ✓ [negative   ] forbidden_voice_clone               | stopped on forbidden pattern
  ✓ [negative   ] pii                                 | PII signal raised
  ✓ [negative   ] destructive                         | confirm required
  ✓ [negative   ] ambiguous                           | 2 decisions handled
  ✓ [negative   ] self_contradiction                  | treated as fact (acceptable)
  ✓ [recovery   ] resume_after_kill                   | resumed and completed after kill
  ✓ [recovery   ] session_persistence                 | 2 facts persisted across restart
  ✓ [regression ] legacy_smoke                        | legacy smoke ok
  ✓ [acceptance ] no_lark_dependency                  | complete flow without lark
  ✓ [acceptance ] resume_after_kill                   | resumed, stage advanced
  ✓ [acceptance ] no_useless_ask                      | 0 ask (acceptable)
  ✓ [acceptance ] high_risk_no_unauthorized           | profile.archive required confirm
  ✓ [acceptance ] tool_failure_detected               | failure detected: FAILED
  ✓ [acceptance ] no_stage_skip                       | stage not advanced prematurely
  ✓ [acceptance ] no_archive_without_confirm          | profile NOT written without confirm_risk

================================================================================
  Total: 21, PASS=21, FAIL=0
================================================================================
  golden       : 1/1
  boundary     : 5/5
  negative     : 5/5
  recovery     : 2/2
  regression   : 1/1
  acceptance   : 7/7
```

### 3.2 老 213 测试不挂

```
$ python3 -m pytest soundmirror/tests -q --ignore=soundmirror/tests/eval_v0
213 passed, 2 skipped in 29.50s
```

## 4. 架构图

```
                         ┌────────────────────────────────────────────┐
                         │          SoundMirror Agent Core v0          │
                         └────────────────────────────────────────────┘
                                          │
                  ┌───────────────────────┼───────────────────────┐
                  ▼                       ▼                       ▼
        ┌──────────────────┐   ┌──────────────────┐    ┌──────────────────┐
        │  InputAdapter    │   │   AgentCore      │    │   ToolAdapter    │
        │                  │   │                  │    │                  │
        │  CliInput        │──▶│  handle(input)   │───▶│  MusicTool       │
        │  ScriptedInput   │   │                  │    │  LarkTool        │
        │  [LarkInput]     │   │  ├─ SignalExtract│    │  [后续]          │
        │  [WebInput]      │   │  ├─ Risk         │    │                  │
        └──────────────────┘   │  ├─ Evidence     │    └──────────────────┘
                               │  ├─ AskRules     │             │
                               │  ├─ StagePolicy  │             ▼
                               │  └─ Orchestrator │    ┌──────────────────┐
                               │                  │    │  Mock 实现       │
                               └──────────────────┘    │                  │
                                          │            │  MockMusicAdapter│
                                          ▼            │  MockLarkAdapter │
                               ┌──────────────────┐    └──────────────────┘
                               │   Persistence    │
                               │                  │
                               │  StateStore(SQLite)│
                               │  TraceRecorder   │
                               │  Recovery scan   │
                               └──────────────────┘
```

## 5. 关键模块清单

```
soundmirror/core/
├── types.py               286 lines  AgentDecision / Evidence / RiskSignal / AskRule / TraceStep 等
├── stage_policy.py        274 lines  7 阶段 can_enter / is_complete / next_stage
├── ask_rules.py           332 lines  8 询问规则(可检查,不是 prompt)
├── evidence.py            370 lines  8 字段评估器(STRONG/MEDIUM/WEAK/ABSENT)
├── risk.py                327 lines  forbidden/PII/action/stage/consistency 五类风险
├── signal_extractor.py    111 lines  intent + text_signals + tool_failures
├── tool_adapter.py        144 lines  Protocol 定义(Music / Lark)
├── music_tool_mock.py     221 lines  MockMusicAdapter + 生成 3 候选包装
├── lark_tool_mock.py       64 lines  MockLarkAdapter(纯内存)
├── agent_core.py         1225 lines  主循环 / _decide / _execute_* / _dispatch_action
├── trace.py               201 lines  TraceRecorder / InMemoryTraceRecorder
├── recovery.py             87 lines  detect_in_flight / verify_clips_on_disk
└── deprecated.py           32 lines  标 8 个 deprecated 模块

soundmirror/inputs/
├── __init__.py            134 lines  CliInput / ScriptedInput
└── scripted.py            198 lines  21 个评测用例 + all_evals / get_eval / make_event

soundmirror/tests/eval_v0/
└── test_core_smoke.py     578 lines  21 用例,5 类 + 7 验收
```

## 6. 关键设计决定

### 6.1 AgentDecision.kind(6 种)

- **continue** — 推进(可能执行 mutation / tool)
- **ask** — 必须等用户回应(明确 missing_fields + ask_options)
- **confirm** — 高影响动作需用户二次确认
- **retry** — 重试上次失败的 tool
- **stop** — 终止
- **escalate** — 超出 Agent 能力

### 6.2 阶段进入 + 完成条件

每个 StagePolicy 是一个 dataclass:
```python
@dataclass
class StagePolicy:
    stage: str
    min_evidence_level: str
    risk_if_incomplete: str
    can_enter: Callable[[SessionState], bool]
    is_complete: Callable[[SessionState], bool]
    next_stage: Optional[str]
```

例:`PROPOSAL_REVIEW` 完成条件 = `lyrics ≥ 30 字符 AND current_proposal 存在`。未达成就 `is_complete=False`,Agent Core 不会 advance。

### 6.3 询问规则(可检查,不是 prompt)

每条 AskRule 是 dataclass:
```python
@dataclass
class AskRule:
    rule_id: str
    field: str
    why: str
    options: List[AskOption]
    when_missing: Optional[str] = None
    priority: int = 0
```

例:`ASK_ARCHAEOLOGY_FIRST` — 当 `current_stage=ARCHAEOLOGY ∧ facts=[]` 时触发,options 包含 "引导" / "例子" / "够了"。

### 6.4 证据 + 风险

**Evidence**:每个字段一个评估器,产出 level + source + confidence + artifact
**Risk**:5 类 forbidden/PII/action/state-consistency/stage-transition,统一产出 `RiskSignal` 列表
**requires_confirm**:HIGH → 必须 confirm;MEDIUM 且 catalog.risk=normal → 不 confirm(catalog 信任);否则 confirm。

### 6.5 Tool Adapter 解耦

```python
class MusicToolAdapter(Protocol):
    name: str
    is_mock: bool
    def health(self) -> bool: ...
    def generate(self, req: MusicGenerationRequest) -> MusicResult: ...
    def generate_full(self, req: FullSongRequest) -> FullSongResult: ...
```

**生产替换路径**:
- `MatrixMusicAdapter` 实现该 Protocol,签名一致
- `YuE2MusicAdapter` 同理
- `AgentCore` 不需改一行

### 6.6 Input Adapter 解耦

```python
class InputAdapter:
    def next_event(self) -> InputEvent: ...
```

**Lark 接入路径**:新增 `LarkInput(InputAdapter)`,把 Lark webhook payload 转 `InputEvent`,Agent Core 不需改一行。

### 6.7 Trace 完整记录

每个 handle() 调用 = 一个 TraceStep,持久化到 SQLite trace 表:
```sql
CREATE TABLE trace (
    trace_id TEXT PRIMARY KEY,
    session_id TEXT,
    step_index INTEGER,
    input_json, stage_before, stage_after,
    decision_json, evidence_json, risk_json,
    tool_calls_json, validation_json, notes_json
)
```

可以 trace_id 检索完整故事;评测时 dump session_trace 看每一步的输入/证据/决策原因/工具调用/验证/状态变化。

## 7. 老代码处理

**复用**(13 个):
- `storage/state_store.py` — 持久化
- `orchestrator/mutation_handlers.py` — 13 stage handler
- `orchestrator/orchestrator.py` — dispatch
- `orchestrator/catalog.py` + `action_catalog.json` — action 校验
- `tools/contracts.py` — Protocol 定义
- `tools/music_pipeline.py::MockMusicPipeline` — 底层 mock
- `tools/lark_sender.py::MockLarkSender` — 飞书 mock
- `tools/audio_clipper.py::MockAudioClipper`
- `tools/asset_uploader.py::MockAssetUploader`

**重构**:
- `director/director.py` → 拆分到 `core/stage_policy.py` + `core/agent_core.py`
- `director/intent_recognition.py` → `core/signal_extractor.py`
- `director/director_v2.py` + `llm_client.py` → 保留作为可选 LLM 注入点
- `chat_repl.py` → 简化为 `inputs/__init__.py::CliInput`

**deprecated**(标 __deprecated__,不删):
- `run_webhook.py` / `start_lark_webhook.sh` — Lark 入口
- `gateway/webhook_server.py` / `card_renderer.py` — Lark 渲染
- `tools/matrix_adapter.py::MatrixMusicAdapter` — 真 Matrix
- `tools/lark_sender.py::HttpLarkSender` — 真 Lark
- `worker/base.py` / `worker/music_worker.py` — 异步 worker(M2)
- `tools/lark_connection_test.py` — Lark 连通性测试

## 8. 后续接入路径

### 8.1 接 Lark

```python
# 1. 实现 LarkInput
class LarkInput(InputAdapter):
    def __init__(self, webhook_payload):
        self.payload = webhook_payload
    def next_event(self) -> InputEvent:
        return parse_lark_to_input_event(self.payload)

# 2. 实现 HttpLarkSender
class HttpLarkSender(LarkToolAdapter):
    def send_card(self, req): ...

# 3. 注入 AgentCore(零改动 core/)
core = AgentCore(
    store=...,
    music_tool=...,
    lark_tool=HttpLarkSender(...),
)
```

### 8.2 接 MiniMax Music / YuE2

```python
class YuE2MusicAdapter:
    name = "YuE2Adapter"
    is_mock = False
    def generate(self, req): ...      # 调 YuE2 API
    def generate_full(self, req): ...

core = AgentCore(store=..., music_tool=YuE2MusicAdapter(...))
```

### 8.3 接异步 worker(M2)

把 `_execute_proposal_confirm` / `_execute_highlight_select` 改成:
```python
# 旧(同步):result = self.music_tool_wrapped.generate_candidates(...)
# 新(异步):job_id = worker.enqueue(generate_candidates, args=(req,))
#         set status=GENERATING, 写 session_job_id
#         return "tool started, will reply when done"
```

Tool callback 走 `input_event.kind = TOOL_CALLBACK`,已经预留。

## 9. 已知限制

1. **同步 tool 调用**:`_execute_proposal_confirm` 是同步的,真实 Matrix 30-180s 时 webhook 会超时。production 需 worker queue。
2. **没有自动 LLM 降级**:目前所有决策都是规则化的。Director V2 的 LLM fallback 没启用。
3. **CLI 简陋**:`CliInput` 是基础 REPL,缺自动补全、历史、tab completion。
4. **trace dump 不完整**:TraceRecorder 持久化了,但没写 dump 工具(评测时直接读 trace 表)。
5. **recovery.detect_in_flight 不自动 retry**:只检测 in-flight,让用户决定 resume。

## 10. 文件清单(本阶段新增)

```
soundmirror/reports/V0_CODE_INVENTORY_2026-09-30.md     9.9KB  盘点
soundmirror/reports/V0_RELEASE_NOTES_2026-09-30.md      本文件  收口

soundmirror/core/__init__.py                            111 lines
soundmirror/core/types.py                               286 lines
soundmirror/core/stage_policy.py                        274 lines
soundmirror/core/ask_rules.py                           332 lines
soundmirror/core/evidence.py                            370 lines
soundmirror/core/risk.py                                327 lines
soundmirror/core/signal_extractor.py                    111 lines
soundmirror/core/tool_adapter.py                        144 lines
soundmirror/core/music_tool_mock.py                     221 lines
soundmirror/core/lark_tool_mock.py                       64 lines
soundmirror/core/agent_core.py                         1225 lines
soundmirror/core/trace.py                               201 lines
soundmirror/core/recovery.py                             87 lines
soundmirror/core/deprecated.py                           32 lines

soundmirror/inputs/__init__.py                          134 lines
soundmirror/inputs/scripted.py                          198 lines

soundmirror/tests/eval_v0/test_core_smoke.py            578 lines

soundmirror/action_catalog.json                         +profile.archive action(4 行)
```

合计新增 / 重构:**~4500 行** Python 代码 + 文档 + 21 个评测用例。
