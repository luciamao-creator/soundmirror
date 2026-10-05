# Changelog

All notable changes to SoundMirror are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [v0.1.0] - 2026-10-05 — Agent Core v0 收口

### 验证结果(53/53)
- ✅ 53/53 assertions pass (11 categories)
- ✅ Real subprocess SIGKILL recovery: 3/3
- ✅ Mutation testing: 3/3 detect mutations
- ✅ Independent recheck: 26/26 consistent
- ✅ Regression: 213/213 tests pass, 2 skipped

### Added — Agent Core v0
- `core/types.py` — `AgentDecision` (6 kinds: continue/ask/confirm/retry/stop/escalate),
  `Evidence`, `RiskSignal`, `AskRule`, `ToolCall`, `TraceStep`, `InputEvent`, `SessionState`
- `core/stage_policy.py` — 7 `StagePolicy` dataclasses with `can_enter` / `is_complete` callables
- `core/ask_rules.py` — 8 `AskRule`s with priority, `why` rationale, options
- `core/evidence.py` — 8 `assess_*` functions, level rank STRONG > MEDIUM > WEAK > ABSENT
- `core/risk.py` — forbidden patterns, PII, action, stage, consistency risk detection
- `core/signal_extractor.py` — user intent classification (fact/edit/confirm/cancel/select/ask)
- `core/tool_adapter.py` — `MusicToolAdapter` / `LarkToolAdapter` Protocols
- `core/music_tool_mock.py` — `MockMusicAdapter` (3 candidates, 220/330/440 Hz)
- `core/lark_tool_mock.py` — `MockLarkAdapter`
- `core/agent_core.py` — `handle(input_event)` main loop, ~1225 lines
- `core/trace.py` — `TraceRecorder` (SQLite, with column-named INSERTs)
- `core/recovery.py` — `detect_in_flight()`, `verify_clips_on_disk()`
- `core/deprecated.py` — explicit `__deprecated__` markers

### Added — Input harness
- `inputs/__init__.py` — `CliInput` (REPL with `:action_id[slot=A]` syntax) + `ScriptedInput` (eval harness)
- `inputs/scripted.py` — 21 fixed evaluation cases in 5 categories

### Added — Evidence harness
- `evidence/collect_v0_evidence.py` — 53 assertions across 11 categories
- `evidence/independent_recheck.py` — 26 independent re-calculations
- `evidence/e10_hard_recovery.py` — real subprocess SIGKILL at steps 2/4/6
- `evidence/verify.sh` — one-command validation
- `evidence/V0_EVIDENCE.json` — claim ledger with full provenance
- `evidence/artifacts/` — 5 SQLite DBs + 4 audio products

### Fixed — 6 real bugs found by evidence collection
- **#1** `core/trace.py:111` — `TraceRecorder.finish()` INSERT 16 列给 15 值,导致 `sqlite3.OperationalError`,trace 表**从未落盘**
- **#2** `core/agent_core.py:846` — `_execute_proposal_confirm` 无条件推进 stage 即使 3 路全失败。改 `overall_status` 分流(FAILED 返 RETRY 停 PROPOSAL_REVIEW)
- **#3** `core/music_tool_mock.py:37` — `_generate_silent_mp3` 全部 anullsrc → 3 候选字节相同。改:按槽位给不同基频(220/330/440 Hz)
- **#4** `core/agent_core.py:309` — `_decide` 空输入走 continue(空转)→ 加 `A0` 信息量守卫,`len < 4` 直接 ASK
- **#5** `action_catalog.json` — 缺 `profile.archive` 入口,补进去
- **#6** `core/agent_core.py:1237` — `_make_decision` 不传递 `tool_calls` → 调用方拿不到 loading 状态。改:加 `tool_calls` 形参 + 所有 return 路径补全

### Documentation
- `reports/V0_CODE_INVENTORY_2026-09-30.md` — 13 reuse / 8 refactor / 11 deprecated
- `reports/V0_RELEASE_NOTES_2026-09-30.md` — initial V0 release summary
- `evidence/REPORTS/V0_VERIFIABLE_DELIVERY_2026-10-05.md` — full evidence disclosure
- `evidence/REPORTS/PM_STATUS_REPORT_2026-10-05.md` — phase-level conclusions

### Disclosed gaps (NOT solved at v0)
- ⚠️ Music quality — mock produces 220/330/440 Hz sine waves, not real music
- ⚠️ No real music backend integration (Matrix / MiniMax Music / YuE2)
- ⚠️ No UI / Lark integration verified
- ⚠️ No concurrency tests
- ⚠️ No real user data
- ⚠️ 100% rule-based decisions, LLM unused
- ⚠️ Long-term memory write-tested, not read-tested in next session
- ⚠️ No multi-turn correction
- ⚠️ No performance baseline
- ⚠️ Compliance is keyword-match, not production-grade

## [v0.0.x] — M0/M1 阶段(2026-09 之前)

Initial M0/M1 implementation. Imports included:
- `gateway/` — feishu webhook server
- `director/` — DirectorV1 + DirectorV2 stubs + llm_client placeholder
- `orchestrator/` — stage state machine
- `stage/` — stage definitions
- `tools/` — matrix_adapter (placeholder), audio_clipper, asset_uploader
- `storage/` — SQLite schema, ledger
- `prompts/` — LLM prompt templates (unused)
- `fixtures/` — test fixtures
- `schemas/` — JSON schemas
- `tests/` — unit (213), integration, eval_v0 (21)
- `cli.py`, `chat_repl.py`, `run_webhook.py`, `cron_bridge.py` — entry points
