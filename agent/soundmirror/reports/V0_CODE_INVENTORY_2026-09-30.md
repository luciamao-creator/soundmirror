# SoundMirror Agent Core v0 — 现有代码盘点

> 日期:2026-09-30
> 阶段:Agent Core v0 收口(暂停飞书/卡片/公网/外部音乐模型)
> 三类清单:复用 / 重构 / 废弃

## 1. 直接复用(不需改)

| 模块 | 行数 | 复用原因 |
|------|------|----------|
| `storage/state_store.py` | 596 | 持久化核心;state_revision + apply_mutation + inbox 三态语义已完成,Agent Core 直接用 |
| `orchestrator/mutation_handlers.py` | 496 | 13 个 stage handler(archaeology/identity/variable/proposal/highlight/full/profile/stage.*)都是纯函数,Agent Core 直接调 |
| `orchestrator/orchestrator.py` | 268 | action dispatch + 风险等级 + confirm 二次确认,设计良好 |
| `orchestrator/catalog.py` + `action_catalog.json` | 14949B | 40 个 action 的元数据 + 校验规则 |
| `tools/contracts.py` | 99 | LarkSender / MusicAdapter / AssetUploader / AudioClipper 接口定义,Adapter 模式已就绪 |
| `tools/lark_sender.py::MockLarkSender` | - | 飞书发送 mock,本阶段不连真实 Lark 时使用 |
| `tools/music_pipeline.py::MockMusicPipeline` | - | 音乐生成 mock(写静默 MP3 + 模拟 preview/full),完全够用 |
| `tools/audio_clipper.py::MockAudioClipper` | - | 音频剪辑 mock |
| `tools/asset_uploader.py::MockAssetUploader` | - | 资源上传 mock(返回伪 URL + checksum) |
| `tools/__init__.py` | - | Tool 导出完整 |

## 2. 需要重构

| 模块 | 重构目标 |
|------|----------|
| `director/director.py` | 拆解为 `StagePolicy`(纯规则,无 prompt)+ `AgentCore`(主循环)。原 13 个 `_handle_*` 阶段函数大多重复 mutation_handlers 职责,移除 |
| `director/intent_recognition.py` | 重构为结构化 `SignalExtractor`(产出 intent + missing_fields + evidence_level + risk_signals),不再 regex + LLM 二选一 |
| `director/director_v2.py` + `llm_client.py` | 保留 LLMClient 抽象(可选注入);director_v2 作为"LLM fallback 策略"封装,不直接挂 stage 处理 |
| `chat_repl.py` | 改成 `InputAdapter::CliInput`,与未来的 `LarkInput` 并列。REPL 协议代码可复用,但移除对 mutation_handlers 的重复注册 |
| `cli.py` | 删掉。重写为新的 `cli_run.py`(CLI 启动入口)+ `core/agent_core.py`(主循环) |
| `gateway/gateway.py` + `gateway/webhook_server.py` | 本阶段不用。但保留 Gateway 作为"可选 transport 适配"接口,后续 Lark 接入时直接复用 |
| `gateway/card_renderer.py` | 标记 deprecated,本阶段不渲染卡片;Agent Core 直接返回 AgentDecision dict,UI 适配由各自实现 |
| `cron_bridge.py` | 思想内化到 Agent Core(每次 handle() 自动持久化 + 启动时自动恢复),不再需要独立 cron 进程 |
| `verify_agent.py` + `inspect_output.py` | 拆解,纳入新的评测集 |
| `prompts/director.md` / `interpreter.md` / `proposal.md` | prompts 不删,但作为"LLM fallback" 输入数据;核心决策不依赖 prompt |

## 3. 应当废弃(标记 deprecated,不删)

| 模块 | 废弃原因 | 后续如何恢复 |
|------|----------|--------------|
| `run_webhook.py` | Lark webhook 入口,本阶段不连 Lark | M3 接入 Lark transport 时,从 git 历史恢复并通过 transport adapter 注入 |
| `start_lark_webhook.sh` | 同上 | 同上 |
| `gateway/webhook_server.py` | 同上 | 同上 |
| `gateway/card_renderer.py` | 飞书卡片渲染,本阶段不需要 | 后续 Lark 接入时直接复用 |
| `tools/matrix_adapter.py::MatrixMusicAdapter` | 真 Matrix API,本阶段用 mock | 切真音乐后端时,只需替换 `MusicToolAdapter` 注入 |
| `tools/matrix_adapter.py::MatrixConnectionError` | 同上 | 同上 |
| `tools/audio_clipper.py::FfmpegAudioClipper` | 真 ffmpeg,本阶段用 mock | 真后端时改回 |
| `tools/asset_uploader.py::MatrixAssetUploader` | 真 OSS 上传,本阶段用 mock | 真后端时改回 |
| `tools/lark_sender.py::HttpLarkSender` | 真 Lark API,本阶段用 mock | 真 Lark 接入时改回 |
| `tools/lark_connection_test.py` | Lark 连通性测试,本阶段不做 | 真 Lark 接入时改回 |
| `worker/base.py` + `worker/music_worker.py` | 异步 worker 队列(M2 设计),本阶段同步 mock 即可 | 真后端或生产部署时再启用 |

**注意**:废弃 ≠ 删除。所有 deprecated 模块在 `core/deprecated.py` 显式 import 一次 + 标注 `__deprecated__ = True`,后续 Lark/Music 接入时不需要重新实现。

## 4. 现状能跑的部分

只跑这些(全部 mock),就能完成"考古 → 身份 → 变量 → 提案 → 片段 → 完整 → 记忆沉淀"完整流程:

```
storage.StateStore                      # 持久化
orchestrator.Orchestrator               # action dispatch
orchestrator.mutation_handlers          # 13 个 stage handler
orchestrator.catalog.ActionCatalog      # action 校验
director.intent_recognition             # 简易 intent 分类
tools.MockMusicPipeline                 # 音乐生成 mock
tools.MockLarkSender                    # 飞书发送 mock
```

## 5. 新增(Agent Core v0)

### 5.1 新模块清单

```
soundmirror/core/
├── __init__.py
├── types.py                  # AgentDecision / Evidence / RiskSignal / AskRule / TraceStep
├── stage_policy.py           # 7 个 StagePolicy 规则(进入/完成条件)
├── ask_rules.py              # 询问策略(从 prompt 抽出的可检查规则)
├── evidence.py               # 证据等级评估
├── risk.py                   # 风险等级评估
├── signal_extractor.py       # 输入解析(intent/missing/risk/evidence)
├── tool_adapter.py           # MusicToolAdapter / LarkToolAdapter 接口
├── music_tool_mock.py        # Mock 实现
├── lark_tool_mock.py         # Mock 实现
├── agent_core.py             # 主循环
├── recovery.py               # 中断恢复
├── trace.py                  # 运行轨迹记录
└── deprecated.py             # 一次性 import 所有 deprecated 模块,标 __deprecated__

soundmirror/inputs/
├── __init__.py
├── cli_input.py              # CLI 输入适配
└── scripted_input.py         # 评测集自动驱动输入

soundmirror/tests/
├── core/                     # Agent Core 单元测试
├── evaluation/               # 固定评测集
│   ├── golden/               # 黄金用例
│   ├── boundary/             # 边界
│   ├── negative/             # 负向
│   ├── recovery/             # 失败恢复
│   └── regression/           # 回归
└── legacy/                   # 老测试,保留不动
```

### 5.2 数据契约

```python
# AgentDecision
{
    "kind": "continue" | "ask" | "confirm" | "retry" | "stop" | "escalate",
    "reasoning": str,                    # 决策原因
    "evidence": [Evidence],              # 支持该决策的证据
    "risk_signals": [RiskSignal],        # 风险信号
    "missing_fields": [str],             # kind=ask 时,缺什么
    "ask_options": [AskOption],          # kind=ask 时,选项
    "risk_level": "low" | "medium" | "high",
    "confidence": float,                 # 0-1
    "stage": str,                        # 当前阶段
    "next_stage": str | None,            # 打算推的下一阶段(continue 时)
    "tool_calls": [ToolCall],            # 本轮执行了哪些工具
    "state_revision_before": int,
    "state_revision_after": int,
    "trace_id": str,                     # 关联到 TraceStep
}
```

### 5.3 7 个阶段

| # | 阶段 | 进入条件 | 完成条件 | 默认证据等级 | 默认风险 |
|---|------|----------|----------|--------------|----------|
| 1 | ARCHAEOLOGY | session 已创建 | archaeology_facts ≥ 2 条且每条 ≥ 4 字符 | strong | low |
| 2 | IDENTITY_FITTING | archaeology 完成 | identity_selection 已记录 | medium | low |
| 3 | VARIABLE_SETUP | identity 完成 | music_spec 4 个字段(bpm/key/genre/instruments)都已设 | medium | low |
| 4 | PROPOSAL_REVIEW | variables 完成 | current_proposal.lyrics ≥ 30 字符 | strong | medium |
| 5 | HIGHLIGHT_REVIEW | proposal 完成 | highlight_clips 中 ≥ 1 个 status=READY | tool | high |
| 6 | FULL_REVIEW | highlight 已选 | selected_highlight.full_song_path 存在且 sha256 已校验 | tool | medium |
| 7 | ARCHIVE | full review 通过 | personal_style_profile 已写入 | strong | high |

### 5.4 Agent Core 主循环

```python
def handle(self, input_event: InputEvent) -> AgentDecision:
    # 1. 恢复 session(可能从中断恢复)
    session = self._resume_or_init(input_event.actor_user_id, input_event.chat_id)
    
    # 2. 抽取信号
    signals = self.signal_extractor.extract(input_event, session)
    
    # 3. 评估当前 stage 进入条件
    policy = self.stages[session.current_stage]
    if not policy.can_enter(session):
        return self._escalate_or_advance(session, signals, "stage_entry_blocked")
    
    # 4. 评估完成条件
    if policy.is_complete(session):
        next_stage = self._next_stage(session.current_stage)
        # 进入下一阶段(可能 ask 确认)
        return self._advance(session, next_stage, signals)
    
    # 5. 评估 ask 规则
    asks = self.ask_rules.evaluate(session, signals)
    if asks and not input_event.continue_yes_no:
        return self._ask(session, asks)
    
    # 6. 评估证据 + 风险
    evidence = self.evidence.assess(signals, session)
    risk = self.risk.assess(evidence, session)
    
    # 7. 决策
    if risk.level == "high" and not input_event.confirm_risk:
        return self._confirm_required(session, risk)
    if evidence.min_level_below(policy.evidence_min):
        return self._ask_more_evidence(session, evidence)
    
    # 8. 执行动作(可能是 tool 调用或 mutation)
    result = self._execute(input_event, session, signals)
    
    # 9. 记录 trace + 持久化
    self.trace.record(...)
    self.store.checkpoint(session)
    
    return result
```
