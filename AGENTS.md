# AGENTS.md — 给 AI 的项目说明书

> **目标读者**:Mavis Agent(我)、未来的 Agent、任何进入本仓库的 AI 助手  
> **目的**:让 AI 助手在 5 分钟内理解项目结构、约定、约束、调试入口,避免反复询问。  
> **必须先读本文再做修改**

---

## 1. 项目一句话

**SoundMirror / 声镜** —— 在飞书私聊里,帮非专业用户通过样本和对话,**从创作意图走到一首完整歌曲**的 Agent。MVP 阶段。

## 2. 当前阶段

**Milestone 0**:能力预检 + 技术方案撰写。  
**Milestone 1**(即将开始):无音乐生成的流程骨架,用 Mock MusicAdapter。

## 3. 仓库地图

```
/workspace/voice-persona-assessment/
├── AGENTS.md                    ← 你正在读
├── prd-versions/
│   ├── PRD_V1.3.md              ← 用户发的 PRD 原文
│   └── PRD_V1.3_REVIEW.md      ← 我的评审笔记
├── design/
│   ├── TECHNICAL_DESIGN_V1.0.md ← 技术方案主文档(权威)
│   └── AGENT_DESIGN_AUDIT.md    ← 10 准则审计
├── demo/song/                    ← 历史演示素材(V1/V2/V3 + 节奏变体)
├── validations/                  ← 9 个能力验证脚本
├── agent/soundmirror/            ← 【待建】Mavis Agent skill
│   ├── SKILL.md                 ← skill 入口
│   ├── prompts/                 ← 提示词(版本化管理)
│   ├── schemas/                 ← JSON Schema
│   ├── tools/                   ← single-purpose tools
│   └── tests/                   ← 单元 + golden cases
├── gateway/                      ← 【待建】Channel Gateway
├── mocks/                        ← 【待建】Mock 服务
├── tests/                        ← 【待建】端到端测试
├── SUMMARY.md                    ← 历史:可行性汇总
├── WORKFLOW.md                   ← 历史:工作流方法论
├── CAPABILITY_REPORT.md          ← 历史:能力实测
├── CARD_INTERACTION_DESIGN.md    ← 历史:卡片设计
└── MUSIC_PERSONA_AGENT_CONFIG.md ← 【已不准确】需要按 V1.0 重构
```

## 4. 命名约定(必须遵守)

| 类型 | 格式 | 示例 |
|---|---|---|
| 飞书 chat_id | `oc_xxxxx` | `oc_a1b2c3d4e5` |
| 飞书 open_id | `ou_xxxxx` | `ou_x9y8z7w6v5` |
| product_user_id | `u_<uuid12>` | `u_a1b2c3d4e5f6` |
| mavis_session_id | 由 Mavis 分配 | (直接用 Mavis 返回) |
| creative_session_id | `cs_<uuid12>` | `cs_x1y2z3w4v5u6` |
| interaction_id | `ix_<uuid8>` | `ix_a1b2c3d4` |
| action_event_id | `evt_<uuid8>` | `evt_e5f6g7h8` |
| asset_id | `asset_<uuid8>` | `asset_i9j0k1l2` |
| candidate_id | `candidate_<uuid8>` | `candidate_m3n4o5p6` |
| trait_id | `trait_<uuid8>` | `trait_q7r8s9t0` |
| state_revision | 整数,自增,从 0 开始 | `12` |

## 5. 关键约定(必须遵守,违反就 fail)

### 5.1 数据层

- **不要相信按钮 payload 里的 session_id / user_id** —— 必须从 Gateway 服务端映射取
- **`extra_data_json` 必须 read-merge-write**,带 `state_revision` 乐观锁,详见 §5.6 TECH_DESIGN_V1.0
- **未知字段必须保留**,不要 strip 任何你没见过的 key
- **未确认信息标 `system_default` 或 `agent_inferred`**,不准标 `user_confirmed`

### 5.2 卡片层

- **不要在卡片内塞音频**,音频走独立的飞书音频消息
- **不要用 URL 跳转按钮完成核心流程**
- **不要在 Generation Card 加取消按钮**(matrix 不支持)
- **不要每轮都发完整 Ledger**,优先 update 旧卡,只在 stage 切换和用户主动查看时发完整

### 5.3 Agent 层

- **Creative Director 只产 InteractionEnvelope**,不准输出飞书 JSON
- **所有 action_id 必须经过白名单路由**(详见 §4.5)
- **所有工具必须是 single-purpose**,可单独测试
- **不要自动把一次选择升级为长期人格**,必须用户确认

### 5.4 安全与隐私

- **不要硬编码任何 API 密钥、token、cookie** —— 一律走 `os.environ`
- **不要在日志里记私人音频内容或飞书凭证**
- **不要把 `unknown` 推断成模型名后写成事实** —— 不确定就写 `unknown`
- **临时 URL(1 小时 upload-temp-url / 2 天 matrix signed URL)不得作为长期存储**

## 6. 如何做常见改动

### 6.1 加一个新的 StageHandler

```
1. 在 agent/soundmirror/stages/ 加一个文件,例如 revision.py
2. 实现 StageHandler Protocol:
   - stage: CreativeStage 属性
   - enter() 返回 InteractionEnvelope
   - on_action(event) 返回 (new_session, envelope_or_none)
3. 在 §4.4 STAGE_ROUTER 注册
4. 在 tests/stages/ 加单元测试 + golden case
5. 改 TECHNICAL_DESIGN_V1.0.md §4.4 路由表
```

### 6.2 加一个新的 InteractionEnvelope 卡片类型

```
1. 在 §5.3 schema 加 card_type 枚举值
2. 在 agent/soundmirror/prompts/director.md 教 LLM 何时用这个 card_type
3. 在 gateway/lark_renderer.py 加对应的 schema
4. 写 snapshot test(golden case)
```

### 6.3 加一个新的 Single-Purpose Tool

```
1. 在 agent/soundmirror/tools/ 加文件,例如 highlight_extractor.py
2. 函数签名:async def tool_name(spec: TypeA) -> TypeB
3. 不准在工具里调 LLM(工具是 deterministic 的)
4. 单元测试 + golden case
```

### 6.4 改提示词

```
1. 改对应文件(director.md / interpreter.md / proposal.md)
2. 文件头版本号同步更新:<!-- version: 1.0.1 -->
3. 在 prompts/CHANGELOG.md 加一行:日期 + 改动 + 影响评估
4. 跑全部 golden cases(prompts/CHANGELOG.md 顶部有命令)
5. 任何 golden case fail → 改回或更新该 case(改 case 必须写理由)
```

### 6.5 加一个新的 Schema

```
1. 在 agent/soundmirror/schemas/ 加 JSON Schema 文件
2. 命名:kebab-case,例如 interaction-envelope.json
3. 必须有: $schema, $id, type, properties, required
4. 在 TECHNICAL_DESIGN_V1.0.md §5 加引用
```

## 7. 调试入口

```bash
# 单元测试
pytest agent/soundmirror/tests/ -v

# 端到端 happy path
pytest tests/e2e/test_happy_path.py -v

# 卡片快照测试
pytest tests/snapshots/ -v --snapshot-update

# State machine 覆盖
pytest tests/state_machine/ -v --cov=agent

# Golden cases(改 prompt 后必跑)
pytest tests/golden/ -v
```

```python
# 调试 session ledger
from agent.soundmirror.session import get_session
session = get_session("cs_xxx")
print(session.ledger)

# 调试当前 stage
print(session.ledger["creative_session"]["stage"])

# 强制跳到某 stage(开发用)
session.set_stage("HIGHLIGHT_REVIEW")
```

## 8. 不要做的事(明确剔除)

- ❌ 不要实现 voice clone(法律 + 矩阵不支持)
- ❌ 不要做 partial regen / stems(矩阵不支持,M2 后再说)
- ❌ 不要相信 matrix 返回的 BPM / 调性是实测值(模型可能没遵守)
- ❌ 不要把用户上传的人声作为生成主音色条件
- ❌ 不要把 1 个工具写成"啥都能干"
- ❌ 不要在 commit 里出现任何 .env 内容或 secrets
- ❌ 不要重构 §5 数据模型而不更新 §5(必须先改文档,再改代码)
- ❌ 不要跳过测试就提交
- ❌ 不要把"我不确定"写成"应该是"

## 9. 当前阻塞项(暂未解决)

1. **CAP-17 私人长期音频存储** —— mavis drive 上传 API 未验证
2. **飞书机器人凭证** —— 需要用户在 open.larksuite.com 申请,5 分钟
3. **真实 Matrix 切换路径** —— M1 用 Mock,M2 验证

## 10. 找谁

- 用户 = PM,改 PRD 找他
- 我(Mavis)= 实现,改代码找我
- **遇到 PRD 没规定的事 → 不要猜,问用户**
- **遇到矩阵能力问题 → 跑 validations/ 下的脚本实测,别凭记忆**

---

**最后更新**:2026-09-16  
**文档版本**:1.0.0
