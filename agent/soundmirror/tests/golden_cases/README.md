# Golden Cases — M1 必跑测试

每个 case 是 **真实用户输入 + 期望的 Agent / Orchestrator 行为**。

## 跑

```bash
pytest agent/soundmirror/tests/golden_cases/ -v
```

18 个 case 必须**全过**才能 merge。任何 prompt 改动前必须跑,改动后也必须跑。

## Case 列表

### 原 5 个(M1 起步)

| # | case_id | 场景 | 关键检验 |
|---|---|---|---|
| 01 | `happy_path_western_road` | 标准 happy path | 走完全部 11 个 stage,生成作品 |
| 02 | `continue_with_persona` | 带历史人格开新歌 | 继承 + 实验维度提示 |
| 03 | `revise_after_review` | 修订完整作品 | 高危动作确认 + budget 消耗 + 版本保留 |
| 04 | `skip_stages` | 跳过多个 stage | source 标记 + 显式默认值可见 |
| 05 | `conflict_in_input` | 输入冲突 | Agent 必须指出冲突,不能静默处理 |

### 新 13 个(V2 评审修订,V1.1 必备)

| # | case_id | 场景 | 对应 V2 评审 |
|---|---|---|---|
| 06 | `action_catalog_consistency` | 所有 prompt 的 Action 都从 catalog 引用 | P0-1 |
| 07 | `ambiguous_ok_does_not_confirm` | "好/OK/嗯" 不作为通用确认 | P1-2 |
| 08 | `ambiguous_slot_requires_clarification` | 裸 "A/B/C" 多义返回 0 candidate + 澄清 | P1-3 |
| 09 | `high_risk_confirmation_executes_once` | 两阶段确认协议 + 防无限循环 | P0-3 |
| 10 | `stale_revision_rejected` | StateMutation 乐观锁拒绝过期 revision | P0-5 |
| 11 | `duplicate_event_no_side_effect` | 同 event_id 重复到达不重复扣预算 | P0-5 |
| 12 | `multi_action_atomicity` | 多动作用 ActionPlan + Orchestrator 校验 | P1-4 |
| 13 | `prompt_injection_in_user_text` | 用户消息含控制指令必须忽略 | P1-6 |
| 14 | `prompt_injection_in_transcript` | ASR 转写中含指令只作素材 | P1-6 |
| 15 | `reference_audio_not_claimed_as_voice_clone` | 不能说"用了你的声音" | PRD 边界 |
| 16 | `generation_schema_retry_does_not_regenerate` | schema 重试不能重发音乐 | P0-6 |
| 17 | `cross_session_isolation` | 多用户数据隔离,不信按钮 payload | P1-6 |
| 18 | `rights_not_confirmed_disables_literal_embed` | 权利未确认禁用 literal_embed | P1-7 |

## 添加新 case

1. 写 `tests/golden_cases/<编号>_<名字>.json`
2. 在本 README 表格加一行
3. 写**故意 fail 用例**(确认新 case 真在工作)

## 注意

- 改 prompt 后:**必须跑全部 18 个老 case**,确认无回归
- 新加 case:**必须解释为什么之前的 prompt 跑不过**(是新能力 / 新边界)
- 不允许"我改了 prompt 所以我加个 case 让它过"——这是作弊

## 文件结构

```
golden_cases/
├── 01_happy_path_western_road.json
├── 02_continue_with_persona.json
├── 03_revise_after_review.json
├── 04_skip_stages.json
├── 05_conflict_in_input.json
├── 06_action_catalog_consistency.json
├── 07_ambiguous_ok_does_not_confirm.json
├── 08_ambiguous_slot_requires_clarification.json
├── 09_high_risk_confirmation_executes_once.json
├── 10_stale_revision_rejected.json
├── 11_duplicate_event_no_side_effect.json
├── 12_multi_action_atomicity.json
├── 13_prompt_injection_in_user_text.json
├── 14_prompt_injection_in_transcript.json
├── 15_reference_audio_not_claimed_as_voice_clone.json
├── 16_generation_schema_retry_does_not_regenerate.json
├── 17_cross_session_isolation.json
├── 18_rights_not_confirmed_disables_literal_embed.json
└── README.md
```
