# SoundMirror / 声镜

> Personal Style Music Agent — 把一个人的语音特征转成他个人风格的音乐。
>
> 当前快照:**2026-10-05 Agent Core v0 收口**

---

## 这个仓库是什么

`SoundMirror` 是一个多阶段 Agent 系统,把用户输入的事实(年龄、性别、口音、用词习惯、性格标签)与编辑意图(换一版、改副歌、不要这种风格)逐阶段对位,最终产出 3 段 30 秒风格样片 + 1 段 90 秒完整歌曲。

**7 个阶段(顺序,门禁式推进,不可跳过):**

```
ARCHAEOLOGY → IDENTITY_FITTING → VARIABLE_SETUP → PROPOSAL_REVIEW
            → HIGHLIGHT_REVIEW → FULL_REVIEW → CONFIRMED → ARCHIVE
   事实采集      身份拟合           变量锁定          提案共审
                                                      ↓
                                                样片评估 → 整曲确认 → 归档
```

每个阶段都有:
- **进入条件** `can_enter`:上一阶段的所有必需素材齐了才能进
- **完成条件** `is_complete`:当前阶段的所有素材齐了才能继续
- **风险拦截** `risk.py`:语音克隆、违规请求等会被拦下来要求二次确认
- **可回放 trace**:每一步决策(用户输入/证据/风险/工具调用/验证结果)落 SQLite

---

## 当前真实状态(2026-10-05 收口)

**已验证(可执行 `bash soundmirror/evidence/verify.sh` 复核):**

- ✅ 53/53 断言通过(artifact/decoupling/discrimination/failure/flow/gate/hard-recovery/mutation/recovery/regression/trace)
- ✅ 真 `kill -9` 中断恢复 3/3(不是同进程模拟)
- ✅ 变异测试 3/3 — 故意把系统改坏,断言立刻失败(证明断言有效)
- ✅ 独立复核 26/26 — 另一套代码重算 sha256/时长/DB 状态,与证据采集器一致
- ✅ 老测试 213/213 通过、2 个 skip、0 失败

**明确没做到(看完整报告 `soundmirror/evidence/REPORTS/PM_STATUS_REPORT_2026-10-05.md`):**

- ⚠️ **音乐质量零证据**:生成的是 220/330/440 Hz 单音,**不是音乐**。无法证明"歌像这个用户"
- ⚠️ 无真实音乐后端(Matrix / MiniMax Music / YuE2 都没接)
- ⚠️ 无 UI 验证(Lark / 飞书模块未跑通)
- ⚠️ 无并发测试
- ⚠️ 无真实用户语料
- ⚠️ 决策 100% 规则化,未启用 `DirectorV2` / LLM
- ⚠️ 合规拦截是关键词匹配,不是生产级

**结论:** 工程底座已可交付,**产品方向是否成立待真实后端接入后验证**。

---

## 仓库结构

```
.
├── README.md                       ← 你正在读
├── .env.example                      ← 凭证占位符(无真实值)
├── .gitignore
├── requirements.txt                ← Python 依赖(实际只有 pytest)
├── AGENTS.md                       ← 工作区约定
├── WORKFLOW.md                     ← 创作流程设计文档
├── MUSIC_PERSONA_AGENT_CONFIG.md   ← 配置说明
├── CAPABILITY_REPORT.md             ← 能力评估
├── CARD_INTERACTION_DESIGN.md      ← Lark 卡片交互设计
├── PROJECT_HANDOFF_PM.md            ← 项目交付文档
├── SUMMARY.md                      ← 项目总结
├── ASSESSMENT.md                   ← 评估材料
│
└── agent/
    └── soundmirror/                 ← 核心代码
        ├── core/                   ← Agent Core v0:类型/门禁/证据/风险/工具适配器
        │   ├── types.py            ← AgentDecision / Evidence / RiskSignal / TraceStep
        │   ├── stage_policy.py     ← 7 个 StagePolicy(can_enter / is_complete)
        │   ├── ask_rules.py        ← 8 条 AskRule(优先级 / why / options)
        │   ├── evidence.py         ← 8 个 assess_*(STRONG > MEDIUM > WEAK > ABSENT)
        │   ├── risk.py             ← 5 类风险检测
        │   ├── signal_extractor.py ← 用户意图分类
        │   ├── tool_adapter.py     ← MusicToolAdapter / LarkToolAdapter Protocol
        │   ├── music_tool_mock.py  ← Mock 实现,3 个可替换
        │   ├── lark_tool_mock.py   ← Mock 实现
        │   ├── agent_core.py       ← handle(input_event) 主循环
        │   ├── trace.py            ← SQLite TraceRecorder
        │   ├── recovery.py         ← detect_in_flight / verify_clips_on_disk
        │   └── deprecated.py       ← __deprecated__ 显式
        ├── director/               ← DirectorV2 + llm_client(占位,未启用)
        ├── orchestrator/           ← mutation_handlers(共享 webhook + bridge)
        ├── gateway/                ← 飞书 webhook server
        ├── inputs/                 ← CliInput / ScriptedInput(评测 harness)
        ├── tools/                  ← matrix_adapter(占位) / audio_clipper / asset_uploader
        ├── stage/                  ← 阶段状态机
        ├── storage/                ← SQLite schema / ledger
        ├── fixtures/               ← 评测 fixture
        ├── prompts/                ← LLM prompt 模板(未启用)
        ├── schemas/                ← JSON schema
        ├── tests/
        │   ├── unit/               ← 单元测试(213 个)
        │   ├── integration/        ← 集成测试
        │   └── eval_v0/            ← Agent Core v0 评测(21 个)
        ├── reports/                ← 阶段报告
        │   ├── V0_CODE_INVENTORY_2026-09-30.md
        │   └── V0_RELEASE_NOTES_2026-09-30.md
        └── evidence/               ← ★ 可验证证据(核心交付)
            ├── verify.sh           ← 一键复核(下面有详情)
            ├── collect_v0_evidence.py
            ├── independent_recheck.py
            ├── e10_hard_recovery.py
            ├── V0_EVIDENCE.json    ← 主证据文件
            ├── REPORTS/
            │   ├── V0_VERIFIABLE_DELIVERY_2026-10-05.md
            │   └── PM_STATUS_REPORT_2026-10-05.md
            └── artifacts/
                ├── db_e1_flow.sqlite    ← 完整流程 session 状态
                ├── db_e1_trace.sqlite   ← 9 步决策轨迹
                ├── e10_kill{2,4,6}.sqlite
                └── audio/               ← 4 个真实音频产物(在 evidence 目录下)
                    ├── highlight_preview_A.mp3
                    ├── highlight_preview_B.mp3
                    ├── highlight_preview_C.mp3
                    ├── full_song_A.mp3
                    └── MANIFEST.json
```

---

## 怎么跑

### 1. 环境要求

```bash
# 系统
python --version    # 需 ≥ 3.11
ffmpeg -version     # 需 ≥ 4.4

# Python 依赖
pip install -r requirements.txt
```

### 2. 一键复核(看 Agent Core v0 是不是真通过)

```bash
cd agent/soundmirror/evidence
bash verify.sh
```

会跑出:
```
断言总数: 53
  通过    : 53
  失败    : 0
独立复核: 26/26 一致
变异测试: 3/3 检测到突变
真 kill : 3/3 恢复
```

预计 90 秒。

### 3. 跑所有测试

```bash
cd agent
python -m pytest soundmirror/tests -q
```

预期:`213 passed, 2 skipped`,约 30 秒。

### 4. 用 CLI 走完整流程

```bash
cd agent
PYTHONPATH=. python3 -m soundmirror.cli --mock
```

输入样例:
```
你的音乐想法
:你偏好慵懒的中文说唱,略带沙哑嗓音
:low/mood=慵懒,high/tempo=85
:u=3
:s=1
:q=exit
```

### 5. 跑评测 harness(21 个固定用例)

```bash
cd agent
PYTHONPATH=. python3 -m soundmirror.inputs.scripted
```

---

## 架构关键决策

- **阶段门禁为数据,不是 prompt** — `StagePolicy(can_enter, is_complete)` 是 dataclass + 可调用函数,可单元测试
- **询问规则为数据** — `AskRule(priority, why, options)` 不是字符串,可独立审视
- **证据有等级** — `STRONG > MEDIUM > WEAK > ABSENT`,缺什么等级就让问答决定
- **风险按 catalog 风险字段分流** — `risk=high` → 必须 confirm,`risk=normal` → 不打断
- **Tool Adapter 是 Protocol** — 换 Music/Lark 后端不改 core,只换实现
- **Trace 落 SQLite** — 每个决策含 input/decision/evidence/risk/tools/validation/state,可回放
- **6 种 AgentDecision** — continue / ask / confirm / retry / stop / escalate
- **inbox 三态** — `received → processing(原子)→ processed`

---

## 已知 bug 清单(本收仓期间修过的)

| # | 位置 | 现象 |
|---|------|------|
| 1 | `core/trace.py:111` | INSERT 列数比占位符少 → trace 表从未落盘 |
| 2 | `core/agent_core.py:846` | 3 路全失败仍进 HIGHLIGHT_REVIEW → 用户拿到坏音频 |
| 3 | `core/music_tool_mock.py:37` | 3 候选字节相同,无法证明参数敏感 |
| 4 | `core/agent_core.py:309` | 空输入走 continue 空转 |
| 5 | `action_catalog.json` | 缺 `profile.archive` / `session.reset_all` |
| 6 | `core/agent_core.py:1237` | `AgentDecision.tool_calls` 永远为空 |

详见 `soundmirror/evidence/REPORTS/V0_VERIFIABLE_DELIVERY_2026-10-05.md`。

---

## 验证策略

不依赖"测试通过"就宣称通过。每个断言要能回答:

1. **断言本身有效吗?** — 变异测试故意改坏代码,断言必须能检测到
2. **采集器的结论可独立复核吗?** — `independent_recheck.py` 用另一套代码重算
3. **不是同进程模拟?** — E10 真起子进程,`os.kill(-9)`
4. **失败不假装成功?** — 注入 3 路全失败,确认不进下一阶段
5. **可回放?** — SQLite trace 含 9 步决策

---

## 复现一次完整的验证流程

```bash
cd agent
# 1. 跑测试
python -m pytest soundmirror/tests -q

# 2. 跑证据采集(53 条断言,真实 SIGKILL,变异测试,真工具调用)
python soundmirror/evidence/collect_v0_evidence.py

# 3. 独立复核
python soundmirror/evidence/independent_recheck.py

# 4. 看报告
cat soundmirror/evidence/REPORTS/PM_STATUS_REPORT_2026-10-05.md
```

---

## 下一步(PM 决策点)

| 优先级 | 内容 | 回答什么问题 |
|--------|------|-------------|
| **A** | 接真实音乐后端,做 10 次完整创作 | "生成的歌像不像用户" — **方向是否成立的唯一答案** |
| B | 找 3–5 个人用 CLI 走完整流程 | "用户觉不觉得有用" |
| C | 补并发测试、trace 回放、性能基线 | 降低返工 |

**强烈建议先做 A。** 缺口 #1 是决定性的:现在的所有证据证明控制流通畅,没证明产品有价值。

---

## 许可证

见 `LICENSE`(TBD)。

## 引用

如果你引用本仓库,请引用:
- 收仓报告:`soundmirror/evidence/REPORTS/V0_VERIFIABLE_DELIVERY_2026-10-05.md`
- PM 状态报告:`soundmirror/evidence/REPORTS/PM_STATUS_REPORT_2026-10-05.md`