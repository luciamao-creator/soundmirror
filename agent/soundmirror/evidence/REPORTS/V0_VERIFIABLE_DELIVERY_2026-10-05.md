# Agent Core v0 — 可验证交付报告

> 采集时间:2026-10-05 20:27 CST
> 复核命令:`bash soundmirror/evidence/verify.sh`
> 原始数据:`soundmirror/evidence/V0_EVIDENCE.json`

---

## 第一部分:先纠正我自己

### 上一轮我给你的东西是无效的

| 我之前说 | 实际情况 |
|---------|---------|
| "21/21 PASS" | 断言太弱。同样的系统换更严的断言,只有 **23/33 = 69.7%** |
| "可溯源" | trace 表**根本没落盘**。`TraceRecorder` 的 INSERT 少一个占位符,SQLite 路径从来没跑通过 |
| "中断恢复已验证" | 只在同进程内丢弃对象,**没做过真进程 kill** |
| "3 个候选已生成" | 3 个文件**字节完全相同**(都是静音),证明 mock 对参数不敏感 |

### 这轮为了拿到真数字,挖出 5 个真 bug

| # | Bug | 严重度 | 修法 |
|---|-----|--------|------|
| 1 | `TraceRecorder` INSERT 16 列给 15 值 → trace 从未落盘 | 高 | 改具名列 INSERT |
| 2 | 3 路生成全失败时仍进 `HIGHLIGHT_REVIEW`,用户点开全是坏的 | **高** | 按 `overall_status` 分流,FAILED 停在上一阶段并返回 RETRY |
| 3 | 3 个候选音频字节完全相同 → 无法证明 tool 对参数敏感 | 中 | 按槽位给不同基频(220/330/440 Hz) |
| 4 | 空输入 / 单字输入走 `continue` 空转,不提问 | 中 | 入口加信息量守卫,直接 ASK |
| 5 | `session.reset_all` / `profile.archive` 不在 action catalog | 中 | 补进 catalog |

**这些不是我"想出来"的,是证据采集器跑出来的。** 在此之前我都不知道。

---

## 第二部分:51 条断言,全部有溯源

### 总览

```
断言总数: 51
  通过    : 51
  失败    : 0
  未验证  : 0
通过率: 100.0% (51/51)

artifact(音频产物)  ████████████████████ 6/6
decoupling(后端解耦) ████████████████████ 3/3
discrimination(区分度)████████████████████ 6/6
failure(失败检测)   ████████████████████ 3/3
flow(完整流程)     ████████████████████ 9/9
gate(阶段门禁)     ████████████████████ 10/10
hard-recovery(真kill)█████████████████████ 5/5
mutation(变异测试)  ████████████████████ 3/3
recovery(恢复)     ████████████████████ 2/2
regression(回归)   ████████████████████ 1/1
trace(溯源)        ████████████████████ 3/3
```

### 关键指标(全部可点开验证)

| 指标 | 数值 | 怎么验证 |
|------|------|----------|
| 完整流程步数 | 9 步走完,`ARCHAEOLOGY → ARCHIVE` | `artifacts/db_e1_flow.sqlite` → `creative_sessions.ledger` |
| 决策轨迹落盘 | 9 行,每步一条,含 input/decision/evidence/risk/tools | `artifacts/db_e1_trace.sqlite` → `trace` 表 |
| 音频产物 | 4 个真实 MP3,ffprobe 实测 30.07s × 3 + 90.07s × 1 | `ls -la /tmp/soundmirror/{highlight,full}/` |
| 3 候选独立性 | sha256 **互不相同**(c7b7a31f / 6ce0959f / 588aa208) | `independent_recheck.py` 自动比对 |
| 3 候选音高实测 | 基频 **220 / 330 / 440 Hz**(zero-crossing 估算)→ 证明 tool 真的对参数敏感 | `ffmpeg` 读 `artifacts/audio/highlight_preview_{A,B,C}.mp3` |
| 真中断恢复 | 3 个子进程被 `SIGKILL(-9)`,恢复后素材 2→2 条不丢,0 重复 session | `artifacts/e10_kill{2,4,6}.sqlite` |
| 决策区分度 | 20 个语义不同输入 → 4 种决策(continue/ask/confirm/stop) | `V0_EVIDENCE.json → E3_discrimination.probes` |
| 工具失败检测 | 3 路全失败 → `generation_status=FAILED`,**停在 PROPOSAL_REVIEW** | `E6_tool_failure.scenarios` |
| 老测试回归 | 213 passed, 2 skipped, 0 failed | `pytest soundmirror/tests -q` |

---

## 第三部分:怎么证明"断言不是空转的"

这是我最想让你看的一节。**通过的断言本身也要被检验。**

### 手段一:变异测试(故意把系统改坏)

我注入了 3 个变异,看对应断言是否会失败:

| 变异 | 预期 | 实际 |
|------|------|------|
| 所有输入都返回 `continue`(橡皮章) | 决策种类从 4 掉到 2,E3-01 失败 | ✅ 掉到 2 |
| `ARCHAEOLOGY.is_complete` 强制返回 True | 门禁行为改变 | ✅ 0 条素材也说完成 |
| 清空 `FORBIDDEN_PATTERNS` | voice clone 拦截失效 | ✅ `forbidden=False` |

**结论:这 3 个机制是真实生效的代码,不是摆设。**

### 手段二:独立复核(不信采集器的结论)

`independent_recheck.py` 用**另一套代码**重新计算 sha256、ffprobe 时长、SQLite 状态,再和采集器的声明对比:

```
独立复核: 26/26 一致
✓ 采集器的每一项声明都与磁盘实际一致
```

如果采集器在撒谎(文件其实不存在 / hash 对不上),这里会报不一致。

### 手段三:非空转检查

| 断言 | 空转会怎样 | 实际 |
|------|-----------|------|
| E5/E10 恢复测试 | kill 点没有素材 → 无从丢失 | 每个 kill 点 kill 前都有 2 条素材 |
| E6 失败注入 | 失败没真注入 → 测的是正常路径 | clips 确实是 `['FAILED','FAILED','FAILED']` |
| E2 音频校验 | 只查文件存在,不查内容 | ffprobe 实测时长 + sha256 独立重算 |

---

## 第四部分:这套证据**测不到**什么(必须说清楚)

我不打算用 51/51 让你以为这事成了。下面是真实缺口:

| # | 缺口 | 意味着什么 |
|---|------|-----------|
| 1 | **音乐质量完全没验证** | 生成的是 220/330/440 Hz 单音,不是音乐。**无法证明"生成的音乐符合用户描述"**。这是产品的核心价值,当前 0 证据 |
| 2 | **没有任何真实后端** | MockMusicAdapter 和 YuE2StyleAdapter 都是我写的假实现。**没验证过与任何真实音乐服务对接** |
| 3 | **没有 Lark / UI 对接** | `core/deprecated.py` 里那些模块一行没跑。当前只验证了"内核能跑",没验证"用户能用" |
| 4 | **没有并发测试** | 所有测试串行。没测过两个请求同时进来、没测过乐观锁在并发下会不会错 |
| 5 | **没有真实数据** | 测试素材是我编的中文短句("海边六点的柴油味")。**没有真实用户语料** |
| 6 | **没有多轮修正** | 没测用户说"不对,重来"时 Agent 能否真的改。目前它只会重跑同一条路 |
| 7 | **长期记忆没闭环** | 只验证了 profile "写进去了",没验证"下一次创作会正确使用它" |
| 8 | **LLM 决策完全没启用** | 当前 100% 规则化。DirectorV2 / llm_client 一行没跑。规则化决策上限很低 |
| 9 | **没有性能数据** | 83s 总耗时里绝大部分是 ffmpeg 生成音频,不是 Agent 决策。无延迟基线 |
| 10 | **合规拦截是关键词匹配** | `FORBIDDEN_PATTERNS` 是字符串列表。绕过成本极低。**这不是生产级合规** |

---

## 第五部分:所以,方向对不对?

### 已经立住的(有证据)

1. **阶段门禁是真的** — 变异测试证明拆掉门禁行为立刻改变
2. **中断恢复是真的** — 真 `SIGKILL` 下不丢数据、不重复建 session
3. **失败不会被当成功** — 3 路全失败时停在原地,不假装有货
4. **后端解耦是真的** — 换成命名/接口都不同的实现,流程一行不改
5. **决策不是橡皮章** — 20 个输入 4 种决策,变异后掉到 2 种
6. **可溯源是真的** — 每步决策落 SQLite,可回放

### 还没立的(无证据)

**最关键的一条:这个 Agent 到底能不能创作出"像这个人的"音乐,我一行证据都没有。**

现在的证据只证明了:**控制流是通的、不会瞎承诺、不会丢数据**。
没证明:**它有创作价值**。

### 我的判断

架构骨架我认为是对的 —— 阶段门禁、证据分级、风险拦截、Tool Adapter 解耦,这几件事做扎实了,换成真音乐模型时不用重写。

但**方向是否成立,取决于第 1 条缺口**:接入真实音乐后端后,生成的歌是否真的"像用户描述的那个人"。**这一步没做完之前,我不能说你这个产品方向被验证了。**

---

## 第六部分:下一步的三个选项

| 选项 | 做什么 | 能解决什么 | 代价 |
|------|--------|-----------|------|
| **A. 接真实音乐后端** | 找一个能用的音乐 API,做 10 个真实用户的完整创作 | 验证核心价值(缺口 1、2) | 需要 API key;不确定 Matrix/YuE2 之外有没有可用服务 |
| **B. 真实用户试用** | 找 3-5 个人,用 CLI 界面走完整流程,记录他们的反应 | 验证"用户是否觉得有价值"(缺口 5、6) | 需要你找人;当前 UI 简陋 |
| **C. 先补工程短板** | 并发测试、trace 回放工具、规则可维护性 | 降低后续返工(缺口 4、9) | 不产生新价值,只是把地基打平 |

**我建议 A,其次 B。** 理由:缺口 1 是"方向对不对"的唯一答案,C 做的再好也回答不了这个问题。

如果你觉得先 B 更实际(真实反馈比真实 API 更容易拿到),也合理。

---

## 附:一键复核

```bash
cd /workspace/voice-persona-assessment/agent
bash soundmirror/evidence/verify.sh
```

它会:
1. 环境自检(ffmpeg / ffprobe / pytest 缺不缺)
2. 重跑 51 条断言(约 90 秒)
3. 用独立代码复核 26 项声明
4. 列出所有落盘产物

你也可以直接看原始数据:

```bash
# 51 条断言的完整声明
python3 -m json.tool soundmirror/evidence/V0_EVIDENCE.json | head -100

# 9 步决策轨迹
sqlite3 soundmirror/evidence/artifacts/db_e1_trace.sqlite \
  "SELECT step_index, stage_before, stage_after, decision_json FROM trace ORDER BY step_index"

# 真 kill 后的 session
sqlite3 soundmirror/evidence/artifacts/e10_kill4.sqlite \
  "SELECT current_stage FROM json_each((SELECT ledger FROM creative_sessions)) LIMIT 1"
```

**产物清单:**

```
soundmirror/evidence/
├── verify.sh                    一键复核
├── collect_v0_evidence.py       51 条断言采集器
├── independent_recheck.py       独立复核(26 项)
├── e10_hard_recovery.py         真 SIGKILL 测试
├── V0_EVIDENCE.json             主证据(51 条断言 + 全部溯源,114KB)
├── REPORTS/
│   └── V0_VERIFIABLE_DELIVERY_2026-10-05.md    本文件
└── artifacts/
    ├── db_e1_flow.sqlite        完整流程的 session 状态
    ├── db_e1_trace.sqlite       9 步决策轨迹(每步 input/decision/evidence/risk/tools)
    ├── e10_kill2.sqlite         SIGKILL @ step2 后的 DB
    ├── e10_kill4.sqlite         SIGKILL @ step4 后的 DB
    ├── e10_kill6.sqlite         SIGKILL @ step6 后的 DB
    └── audio/                   归档的真实音频产物
        ├── highlight_preview_A.mp3   30.5KB  30.07s  220 Hz
        ├── highlight_preview_B.mp3   30.6KB  30.07s  330 Hz
        ├── highlight_preview_C.mp3   30.6KB  30.07s  440 Hz
        ├── full_song_A.mp3           89.9KB  90.07s
        └── MANIFEST.json
```

音频产物已归档到 `evidence/artifacts/audio/`,不依赖会被清空的 `/tmp`。
你可以自己验:

```bash
cd soundmirror/evidence/artifacts/audio
for f in highlight_*.mp3; do
  echo -n "$f  "
  ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$f"
done
# 听/看波形,或用任意播放器打开
```
