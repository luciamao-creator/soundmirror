# Prompts / Skill CHANGELOG

跟踪所有 prompt 文件的版本变更。每次改动必须:
1. 更新对应文件头的 `<!-- version: x.y.z -->`
2. 更新对应文件头的 `<!-- last_updated: YYYY-MM-DD -->`
3. 在此文件加一行
4. 同步更新 `action_catalog.json`(如新增 action)

## 格式

```
## YYYY-MM-DD — file_name
- version: x.y.z
- summary: 一句话改动
- impact: 影响评估(Golden Cases 全过 / 有新增 / 有回归)
- required_action: 同时改的其他文件
```

---

## 2026-09-17

### SKILL.md
- version: 1.0.0 → 1.1.0
- summary: 引用 action_catalog.json、加两阶段确认协议、AgentResult 分层、StateMutation、不可信输入规则、个人素材权利、引用 M0C 实测报告、删过时 Mavis UI 措辞
- impact: V2 评审 P0 + P1 全部接受(7 P0 + 10 P1)
- required_action: action_catalog.json(新建) / director.md / interpreter.md / proposal.md 同步改

### action_catalog.json
- version: 1.0.0(新建)
- summary: 唯一 Action 注册表,含 36 个 action,4 个风险等级,两阶段确认协议定义
- impact: 解决 V2 评审 P0-1 / P0-2 / P0-3
- required_action: 所有 prompt 文件引用此 catalog

### prompts/director.md
- version: 1.0.0 → 1.1.0
- summary: 输出改为 AgentResult(envelope / mutation / tool_request / events / error)、引用 catalog、高危动作 requires_confirmation、CANDIDATE_GENERATING 阶段用 tool_request 不再让 LLM "等待"
- impact: 解决 V2 评审 P0-4 / P0-6 / P0-7 / P1-5 / P1-7 / P1-9
- required_action: SKILL.md / interpreter.md 同步

### prompts/interpreter.md
- version: 1.0.0 → 1.1.0
- summary: 引用 catalog、模糊语言只保留语义方向不直译具体值、裸 A/B/C 多义返回 0 candidate、"好/OK" 不作通用确认、多动作用 ActionPlan 带原子性校验
- impact: 解决 V2 评审 P1-1 / P1-2 / P1-3 / P1-4
- required_action: SKILL.md / director.md 同步

### prompts/proposal.md
- version: 1.0.0
- summary: 初版(本次未改,V1.1 计划修订)
- impact: 无
- required_action: 无

---

## 下次改动预期

### 预期 v1.2.0(下一步重点)

| 项 | 来源 | 工作量 |
|---|---|---|
| `prompts/proposal.md` 引用 action_catalog | V2 P0-1 | 0.5h |
| 移除所有 prompt 文件中的"自定义 Action 列表"残余 | V2 P0-1 | 0.5h |
| 加入 M0C 实测报告引用完整性 | V2 P1-8 | 0.5h |
| 新增 13 个 Golden Cases(action_catalog_consistency 等) | V2 P1-10 | 4h |

### 预期 v1.3.0(架构层)

| 项 | 来源 | 工作量 |
|---|---|---|
| `PendingAction + confirm.accept/cancel` 数据结构 + Orchestrator 实现 | V2 P0-3 | 3-4h |
| `StateMutation` + AtomicStore 实现 | V2 P0-5 | 4-6h |
| `AgentResult` 分层 + 补全 schema(error / mutation / tool_request) | V2 P0-7 | 2h |
| 真实 `MavisAdapter`(替换 `mavis.call_skill` 伪接口) | V2 P0-4 | 1-2 天 |
| `SoundAsset` schema 含权利 / 隐私字段 | V2 P1-7 | 1h |
