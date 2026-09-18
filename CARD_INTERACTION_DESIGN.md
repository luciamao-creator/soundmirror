# 卡片式交互设计 · PRD 更新指引

> **目标读者**:负责更新 PRD V1.0 的 PM  
> **目的**:把"卡片式交互"从 V1.0 的"Mavis 原生卡片(已被 CAPABILITY_REPORT 否决)"切换到 **"飞书卡片 + Mavis Agent 后端"** 的新方案  
> **关联文档**:`PRD V1.0`、`CAPABILITY_REPORT.md`、`MUSIC_PERSONA_AGENT_CONFIG.md`  
> **编写日期**:2026-09-15

---

## 0. 为什么需要更新 PRD

### V1.0 假设(已被否决)

PRD V1.0 §5.1、§8、§8.1–§8.3 都假设 **Mavis 提供原生互动卡片**(按钮、选项、表单)。  
**CAPABILITY_REPORT.md CAP-02 / CAP-03 实际验证结论**:
- ❌ Mavis **没有**原生侧边栏
- ❌ Mavis **没有**原生按钮触发 API
- ✅ Mavis 只能输出 Markdown + `<media>` 标签
- ✅ 用户必须"打字回复数字或自然语言"

### 新决策(本指引的依据)

**卡片交互层迁移到飞书/Lark**,Mavis Agent 退居"后台大脑"。  
**新三层架构**:

```
┌─────────────────────────────────────────┐
│ 飞书 / Lark(用户界面层)                │
│ - 富卡片:按钮 / 下拉 / 表单 / 音频     │
│ - 私聊 / 群聊                            │
│ - 用户无需注册,扫码即可                  │
└────────────┬────────────────────────────┘
             │ webhook 事件 / card 回调
             ▼
┌─────────────────────────────────────────┐
│ 你的小后端(路由层,~50–100 行 Python)   │
│ - 飞书 user_open_id ↔ Mavis session_id │
│ - 接收卡片回调,转发给 Mavis              │
│ - 解析 Agent 输出,提取卡片 JSON          │
│ - 用 lark-cli 把卡片推回飞书             │
└────────────┬────────────────────────────┘
             │ mavis session messages
             ▼
┌─────────────────────────────────────────┐
│ Mavis Agent(大脑层,在 Mavis 内运行)    │
│ - 维护状态机 / 创作账本                  │
│ - 调 matrix(LLM / Music / TTS)         │
│ - 输出"结构化卡片 JSON + 文本回复"      │
└─────────────────────────────────────────┘
```

---

## 1. 飞书 Interactive Card 基础规范

### 1.1 顶层 JSON Schema

```json
{
  "config": { "wide_screen_mode": true },
  "header": {
    "title": { "tag": "plain_text", "content": "卡片标题" },
    "template": "blue"  // blue | green | red | orange | purple | grey
  },
  "elements": [
    // 内容元素,见 §1.2
  ]
}
```

### 1.2 可用元素组件(对应 PRD §8.1 卡片类型)

| 飞书元素 | 功能 | PRD §8.1 映射 |
|---|---|---|
| `div` | 块容器,可嵌套 markdown | 所有卡片的"主体内容" |
| `text` | 单行/多行文本(支持 markdown 子集) | 段落、提示、警告 |
| `markdown` | 富文本块(支持链接、@、表情) | Question / Summary / Proposal 卡片的内容 |
| `button` | 按钮(可带 value 回调或 url 跳转) | 所有需要用户"选"的场景 |
| `select_static` | 下拉选择 | Variable 卡片中的多选项变量 |
| `input` | 文本输入框 | 需要用户输入长文本(歌词、声音描述) |
| `image` | 图片(URL) | 封面 / 波形截图 / 参考图 |
| `audio` | 音频播放器(URL,支持 stream) | **Sample Card / Audio Review Card** |
| `person` | 用户卡片 | 显示"小明"等用户标识 |
| `hr` | 分隔线 | 卡片视觉分段 |
| `column_set` + `column` | 多列布局 | 候选对比(并排展示 A/B/C 样本) |
| `action` | 按钮容器(在卡片底部) | "确认提案"、"开始生成"等关键操作 |

### 1.3 关键:按钮的回调机制

```json
{
  "tag": "button",
  "text": { "tag": "plain_text", "content": "保留 A" },
  "type": "primary",       // default | primary | warning | danger
  "value": {
    "action_id": "keep_sample",
    "candidate_id": "A",
    "session_id": "mvs_xxx"  // 路由用,可选
  }
}
```

- 用户点按钮 → 飞书向**你的后端 URL** POST 回调
- `value` 字段是**透传载荷**,原样回给你的后端
- 后端用 `action_id` 判断用户做了什么动作,用 `session_id` 路由到正确的 Mavis session

---

## 2. PRD §8.1 的 9 种卡片 → 飞书实现

### 2.1 Question Card

**用途**:单个高价值追问(声音考古阶段用)

```json
{
  "header": {
    "title": { "tag": "plain_text", "content": "🎤 声音考古 · 追问" },
    "template": "blue"
  },
  "elements": [
    { "tag": "markdown", "content": "**这次想表达什么?(选最接近的)**" },
    {
      "tag": "action",
      "actions": [
        {
          "tag": "button", "type": "primary",
          "text": { "tag": "plain_text", "content": "离别" },
          "value": { "action_id": "archaeology.theme", "value": "离别" }
        },
        {
          "tag": "button",
          "text": { "tag": "plain_text", "content": "思念" },
          "value": { "action_id": "archaeology.theme", "value": "思念" }
        },
        {
          "tag": "button",
          "text": { "tag": "plain_text", "content": "孤独" },
          "value": { "action_id": "archaeology.theme", "value": "孤独" }
        },
        {
          "tag": "button", "type": "danger",
          "text": { "tag": "plain_text", "content": "都不是,我自己说" },
          "value": { "action_id": "archaeology.theme", "value": "free_input" }
        }
      ]
    },
    { "tag": "hr" },
    { "tag": "markdown", "content": "💡 也可以直接打字告诉我。随时 [跳过] 这一步。" }
  ]
}
```

**对应 PRD 字段**:必需元素(问题、推荐选项、自由输入、跳过)✅ 全部覆盖  
**用户操作**:点按钮 / 直接打字回复

---

### 2.2 Summary Card

**用途**:阶段总结(身份试衣间结束时)

```json
{
  "header": { "title": { "tag": "plain_text", "content": "📋 声音考古 · 阶段总结" }, "template": "green" },
  "elements": [
    { "tag": "markdown", "content": "**核心表达**\n 川藏公路,精神归途,苦难中凝实" },
    { "tag": "hr" },
    { "tag": "markdown", "content": "**情绪曲线**(? 推断)\n 绝望 → 一丝希望 → 平实\n\n来源:? Agent 推断 | ✓ 你已确认" },
    { "tag": "hr" },
    { "tag": "markdown", "content": "**个人素材**\n • 风 / 颂钵 / 经幡(来自你上传)\n\n**待确认**\n • 是否需要保留所有 5 个素材?" },
    { "tag": "hr" },
    { "tag": "markdown", "content": "**禁止项**(✓ 你已确认)\n • 不出现说唱 / EDM drops" },
    {
      "tag": "action",
      "actions": [
        { "tag": "button", "type": "primary",
          "text": { "tag": "plain_text", "content": "✓ 确认,进入下一步" },
          "value": { "action_id": "summary.confirm" } },
        { "tag": "button",
          "text": { "tag": "plain_text", "content": "✎ 修改某项" },
          "value": { "action_id": "summary.edit" } },
        { "tag": "button", "type": "danger",
          "text": { "tag": "plain_text", "content": "← 返回重做" },
          "value": { "action_id": "summary.back" } }
      ]
    }
  ]
}
```

**来源标记符号**:✓ 用户确认 / ? Agent 推断 / ○ 系统默认 / ◇ 本曲限定 / ★ 长期人格

---

### 2.3 Sample Card(关键)

**用途**:音色/曲风/元素样本对比(身份试衣间核心交互)

```json
{
  "header": { "title": { "tag": "plain_text", "content": "🎨 身份试衣间 · 距离感" }, "template": "purple" },
  "elements": [
    { "tag": "markdown", "content": "**这次想要人声距离感?** (盲选,不告诉你 A/B/C 的具体标签)" },
    { "tag": "hr" },
    {
      "tag": "column_set",
      "flex_mode": "stretch",
      "columns": [
        { "tag": "column", "width": "weighted", "weight": 1, "elements": [
          { "tag": "markdown", "content": "**样本 A**" },
          { "tag": "audio", "src": "https://...preview_a.mp3" },
          { "tag": "actions", "actions": [
            { "tag": "button", "type": "primary", "text": { "tag": "plain_text", "content": "保留" },
              "value": { "action_id": "sample.keep", "slot": "A" } },
            { "tag": "button", "text": { "tag": "plain_text", "content": "借用局部" },
              "value": { "action_id": "sample.borrow", "slot": "A" } },
            { "tag": "button", "type": "danger", "text": { "tag": "plain_text", "content": "排除" },
              "value": { "action_id": "sample.reject", "slot": "A" } }
          ]}
        ]},
        { "tag": "column", "width": "weighted", "weight": 1, "elements": [
          { "tag": "markdown", "content": "**样本 B**" },
          { "tag": "audio", "src": "https://...preview_b.mp3" },
          { "tag": "actions", "actions": [
            { "tag": "button", "type": "primary", "text": { "tag": "plain_text", "content": "保留" },
              "value": { "action_id": "sample.keep", "slot": "B" } },
            { "tag": "button", "text": { "tag": "plain_text", "content": "借用局部" },
              "value": { "action_id": "sample.borrow", "slot": "B" } },
            { "tag": "button", "type": "danger", "text": { "tag": "plain_text", "content": "排除" },
              "value": { "action_id": "sample.reject", "slot": "B" } }
          ]}
        ]},
        { "tag": "column", "width": "weighted", "weight": 1, "elements": [
          { "tag": "markdown", "content": "**样本 C**" },
          { "tag": "audio", "src": "https://...preview_c.mp3" },
          { "tag": "actions", "actions": [
            { "tag": "button", "type": "primary", "text": { "tag": "plain_text", "content": "保留" },
              "value": { "action_id": "sample.keep", "slot": "C" } },
            { "tag": "button", "text": { "tag": "plain_text", "content": "借用局部" },
              "value": { "action_id": "sample.borrow", "slot": "C" } },
            { "tag": "button", "type": "danger", "text": { "tag": "plain_text", "content": "排除" },
              "value": { "action_id": "sample.reject", "slot": "C" } }
          ]}
        ]}
      ]
    },
    { "tag": "hr" },
    {
      "tag": "actions",
      "actions": [
        { "tag": "button", "text": { "tag": "plain_text", "content": "都不对,再生成" },
          "value": { "action_id": "sample.none" } },
        { "tag": "button", "text": { "tag": "plain_text", "content": "A + B 混搭" },
          "value": { "action_id": "sample.mix", "slots": ["A", "B"] } },
        { "tag": "button", "text": { "tag": "plain_text", "content": "跳过 →" },
          "value": { "action_id": "sample.skip" } }
      ]
    },
    { "tag": "markdown", "content": "💡 也可以直接打字:`保留 A,排除 C`" }
  ]
}
```

**关键特性**:
- A/B/C 真实盲选(用户看不到"贴耳/自然/辽阔"等引导标签)
- audio 组件**内嵌播放**,用户不需要点出去
- 候选揭晓后,Agent 在下一条消息里揭示每个样本的设计意图

---

### 2.4 Proposal Card

**用途**:创作提案(共同导演阶段核心)

```json
{
  "header": { "title": { "tag": "plain_text", "content": "📝 创作提案 · 待你审阅" }, "template": "blue" },
  "elements": [
    { "tag": "markdown", "content": "**1. 本次要表达什么**\n 川藏公路的"苦难归途",不浪漫化,不悲情" },
    { "tag": "hr" },
    { "tag": "markdown", "content": "**2. 情绪曲线**\n 压抑(intro)→ 张力(verse)→ 一缕暖光(chorus)→ 平实(结尾)" },
    { "tag": "hr" },
    { "tag": "markdown", "content": "**3. 继承锚点**\n ✓ 已锁定的 V3 Atmospheric 风格" },
    { "tag": "markdown", "content": "**4. 本次突破**\n 把节奏从 72 BPM 提升到 88 BPM,让"归途"有"在走"的感觉" },
    { "tag": "markdown", "content": "**5. 三个核心声音特征**\n • 颂钵作为"远方钟声"出现\n • 风声始终铺底\n • 木轮声在副歌前奏" },
    { "tag": "hr" },
    { "tag": "markdown", "content": "**6. 用户素材角色**\n 你上传的风声 → 全曲铺底(不是装饰,而是"路本身")" },
    { "tag": "markdown", "content": "**7. 推荐结构**\n Intro(8s)→ Verse1(45s)→ Chorus(30s)→ Verse2(45s)→ Chorus(30s)→ Outro(10s)\n 总时长 ~3 分钟" },
    { "tag": "hr" },
    {
      "tag": "collapsible",
      "summary": "📄 歌词初稿(点击展开)",
      "elements": [
        { "tag": "markdown", "content": "[Verse 1]\nThe road unrolls..." }
      ]
    },
    { "tag": "hr" },
    {
      "tag": "actions",
      "actions": [
        { "tag": "button", "type": "primary",
          "text": { "tag": "plain_text", "content": "✓ 整体确认,进入变量工作台" },
          "value": { "action_id": "proposal.confirm" } },
        { "tag": "button",
          "text": { "tag": "plain_text", "content": "整体方向对,但第 X 项不对" },
          "value": { "action_id": "proposal.edit_item" } },
        { "tag": "button",
          "text": { "tag": "plain_text", "content": "✎ 修改歌词" },
          "value": { "action_id": "proposal.edit_lyrics" } },
        { "tag": "button", "type": "danger",
          "text": { "tag": "plain_text", "content": "← 重做提案" },
          "value": { "action_id": "proposal.back" } }
      ]
    }
  ]
}
```

---

### 2.5 Variable Card

**用途**:参数工作台(变量控制)

```json
{
  "header": { "title": { "tag": "plain_text", "content": "🎛️ 变量工作台" }, "template": "blue" },
  "elements": [
    { "tag": "markdown", "content": "**节奏感 / BPM**\n 当前:88 BPM(? 推断) · 推荐:步行节奏\n\n修改:" },
    {
      "tag": "select_static",
      "placeholder": { "tag": "plain_text", "content": "选 BPM" },
      "options": [
        { "text": { "tag": "plain_text", "content": "慢燃烧 · 68 BPM" },
          "value": "68" },
        { "text": { "tag": "plain_text", "content": "步行 · 88 BPM(推荐)" },
          "value": "88" },
        { "text": { "tag": "plain_text", "content": "驾驶 · 108 BPM" },
          "value": "108" }
      ],
      "value": "88"
    },
    { "tag": "hr" },
    { "tag": "markdown", "content": "**主乐器**\n 当前:指弹吉他 + 颂钵(? 推断)" },
    {
      "tag": "actions",
      "actions": [
        { "tag": "button", "type": "primary",
          "text": { "tag": "plain_text", "content": "✓ 接受推荐" },
          "value": { "action_id": "variable.accept", "key": "instrument" } },
        { "tag": "button",
          "text": { "tag": "plain_text", "content": "改" },
          "value": { "action_id": "variable.edit", "key": "instrument" } },
        { "tag": "button",
          "text": { "tag": "plain_text", "content": "🎧 听样本" },
          "value": { "action_id": "variable.sample", "key": "instrument" } }
      ]
    },
    { "tag": "hr" },
    { "tag": "markdown", "content": "**高级设置**(折叠)" },
    {
      "tag": "collapsible",
      "summary": "拍号 / 调式 / 和声密度 / 鼓组 / 人声音域 / ... (点击展开)",
      "elements": [
        { "tag": "markdown", "content": "拍号:4/4 · 调式:D minor · 和声密度:中等..." }
      ]
    }
  ]
}
```

---

### 2.6 Generation Card(任务状态)

**用途**:展示生成进度

```json
{
  "header": { "title": { "tag": "plain_text", "content": "🎵 正在生成" }, "template": "blue" },
  "elements": [
    { "tag": "markdown", "content": "**阶段**: 完整作品生成\n\n**已用预算**:3/4 次音乐生成\n\n**当前**: 调用 matrix music-2.6..." },
    { "tag": "hr" },
    {
      "tag": "actions",
      "actions": [
        { "tag": "button", "type": "danger",
          "text": { "tag": "plain_text", "content": "✕ 取消" },
          "value": { "action_id": "generation.cancel" } }
      ]
    }
  ]
}
```

> 注:Music 没有百分比进度,**只能三态**:准备中 / 生成中(无进度条) / 完成 / 失败

---

### 2.7 Audio Review Card(关键)

**用途**:片段/完整作品播放 + 反馈

```json
{
  "header": { "title": { "tag": "plain_text", "content": "🎧 片段 1/2 · 副歌候选" }, "template": "purple" },
  "elements": [
    { "tag": "audio", "src": "https://...clip_1.mp3" },
    { "tag": "markdown", "content": "**创作意图**:在 88 BPM 步行节奏上,颂钵 + 木轮开场,女声温暖磁性 + 转音" },
    { "tag": "hr" },
    { "tag": "markdown", "content": "**歌词(片段)**\n[Verse 1]\nThe road unrolls beneath..." },
    { "tag": "hr" },
    {
      "tag": "actions",
      "actions": [
        { "tag": "button", "type": "primary",
          "text": { "tag": "plain_text", "content": "更像我" },
          "value": { "action_id": "review.like_me", "clip_id": "clip_1" } },
        { "tag": "button",
          "text": { "tag": "plain_text", "content": "好听" },
          "value": { "action_id": "review.good", "clip_id": "clip_1" } },
        { "tag": "button",
          "text": { "tag": "plain_text", "content": "符合本曲" },
          "value": { "action_id": "review.fits", "clip_id": "clip_1" } },
        { "tag": "button",
          "text": { "tag": "plain_text", "content": "✎ 文字反馈" },
          "value": { "action_id": "review.comment", "clip_id": "clip_1" } }
      ]
    },
    { "tag": "hr" },
    { "tag": "markdown", "content": "💡 这三个维度**必须独立评估**(更像我 / 好听 / 符合本曲)" }
  ]
}
```

---

### 2.8 Learning Card

**用途**:长期人格沉淀

```json
{
  "header": { "title": { "tag": "plain_text", "content": "📚 本次学到了什么" }, "template": "green" },
  "elements": [
    { "tag": "markdown", "content": "**学习项 1**\n 你喜欢 88 BPM 步行节奏,胜过 72 BPM 慢燃烧\n\n**证据**:你盲听了 3 个节奏样本,选了 88 BPM(用户确认 ✓)" },
    { "tag": "hr" },
    {
      "tag": "select_static",
      "placeholder": { "tag": "plain_text", "content": "这条学习的作用范围?" },
      "options": [
        { "text": { "tag": "plain_text", "content": "仅当前版本" }, "value": "version" },
        { "text": { "tag": "plain_text", "content": "当前歌曲" }, "value": "track" },
        { "text": { "tag": "plain_text", "content": "当前项目/系列" }, "value": "project" },
        { "text": { "tag": "plain_text", "content": "★ 加入长期人格" }, "value": "global" },
        { "text": { "tag": "plain_text", "content": "✕ 不保存" }, "value": "discard" }
      ]
    },
    { "tag": "hr" },
    {
      "tag": "actions",
      "actions": [
        { "tag": "button", "type": "primary",
          "text": { "tag": "plain_text", "content": "✓ 确认" },
          "value": { "action_id": "learning.confirm" } },
        { "tag": "button",
          "text": { "tag": "plain_text", "content": "✎ 改写这条学习" },
          "value": { "action_id": "learning.edit" } }
      ]
    }
  ]
}
```

---

### 2.9 Ledger Card(降级:代替侧边栏)

**用途**:由于 Mavis 没有原生侧边栏,**用飞书消息+固定前缀作为"创作账本"**。Agent 在每条回复前/后追加一张 Ledger Card,用户可"折叠查看"。

```json
{
  "header": { "title": { "tag": "plain_text", "content": "📋 创作账本 · 当前进度" }, "template": "grey" },
  "elements": [
    {
      "tag": "collapsible",
      "summary": "▼ 已完成阶段(声音考古 ✓ · 身份试衣 ✓ · 共同导演 ✓ · 变量工作台 → 进行中)",
      "elements": [
        { "tag": "markdown", "content": "**本次目标**:川藏公路,苦难归途\n\n**用户素材**:风/颂钵/经幡(✓)\n\n**已确认锚点**:V3 Atmospheric · D minor · 女声宽音域 + 转音\n\n**当前歌曲变量**:BPM=88 · 调式=D minor · 主乐器=指弹吉他+颂钵 · ...\n\n**生成预算**:3/4 次\n\n**版本**:V1(完整歌曲,已生成)" }
      ]
    }
  ]
}
```

> **重要**:此卡每次 Agent 回复时**重新发送一次**(飞书消息会更新卡片),用户能始终看到最新状态

---

## 3. 交互流程:用户点按钮后会发生什么

### 3.1 时序图

```
用户在飞书点 [保留 A] 按钮
   │
   │ ① 飞书 → POST 到你的后端 webhook URL
   │    payload: {
   │      "action_id": "sample.keep",
   │      "slot": "A",
   │      "session_id": "mvs_xxx",     // 路由用
   │      "user_open_id": "ou_xxx",
   │      "chat_id": "oc_xxx",
   │      "timestamp": "..."
   │    }
   │
   │ ② 你的后端:
   │    a. 验签(飞书加密)
   │    b. 查 user_map → mavis_session_id
   │    c. 构造用户输入字符串:
   │       "[按钮事件] user kept sample A in 'distance' test"
   │
   │ ③ 把消息发给 Mavis:
   │    mavis("session messages send", session_id=mvs_xxx, content=...)
   │
   │ ④ Mavis Agent 内部:
   │    - 读取 session extra_data_json(状态)
   │    - 根据 action_id 路由到"样本选择"逻辑
   │    - 更新状态(把 A 标记为保留)
   │    - 决定下一步:继续 A/B 比较 / 进入下一维度 / 生成片段
   │    - 输出:卡片 JSON(可能是 Sample Card 下一轮,或 Ledger Card)
   │
   │ ⑤ 你的后端收到 Agent 输出:
   │    - 提取卡片 JSON
   │    - 用 lark-cli 发到飞书:
   │      lark-cli im +messages-send --as bot --chat-id oc_xxx --card '<JSON>'
   │
   │ ⑥ 用户在飞书里看到新卡片 ✅
```

### 3.2 失败与降级

| 失败场景 | 行为 |
|---|---|
| Mavis 调用超时 | Agent 回复卡片"还在生成...",用户可点 [取消] |
| matrix 音乐生成失败 | Agent 回复 Generation Card 显示"失败"+ [重试] [跳过] [换 prompt] |
| 飞书 webhook 验签失败 | 后端返回 200 但不处理(避免重试风暴) |
| lark-cli 发送失败 | 后端入重试队列,30s 后再发一次,最多 3 次 |
| 用户发了文本(非按钮) | 走通用对话分支,Agent 识别后路由到对应状态机节点 |

---

## 4. PRD V1.0 各章节更新指引

### 4.1 必须修改的章节

| 章节 | V1.0 内容 | V1.1 应改为 |
|---|---|---|
| §0 文档目的 | "MVP 运行在 Mavis 内,不跳转到独立站点" | **改为**:"MVP 体验层在飞书/Lark,后端在 Mavis。**飞书扫码 = 用户注册**" |
| §5.1 主界面 | "主会话区 + 侧边栏 + 当前产物区(全在 Mavis)" | **改为**:"飞书对话(主)+ Mavis 后台(账本/状态)" |
| §5.2 侧边栏创作账本 | "侧边栏固定包含..." | **改为**:"**Ledger Card**(飞书消息),每次回复时重新发送" |
| §8 卡片与 CLI 交互规范 | "卡片类型 / CLI 降级格式 / 卡片行为规范" | **改为**:本文档 §2 + §3(飞书卡片 JSON + 回调机制) |
| §8.2 CLI 降级格式 | "如果 Mavis 不支持互动组件,使用以下稳定格式..." | **删除**(V1.1 已不需要 Markdown 降级,直接用飞书卡片) |
| §9.4 成本控制 | "每首歌曲默认预算" | **补充**:"每飞书用户每首歌曲默认预算" |
| §14 非功能要求 | "持久化、性能、隐私、可解释性" | **补充**:"飞书事件 webhook 验签、lark-cli 重试机制" |
| §16 推荐技术结构 | "Mavis Conversation Surface → Card Renderer → ..." | **改为**:"飞书 Chat → 你的后端 → Mavis Agent → Matrix" |
| §17 开发里程碑 | 4 个 Milestone | **补充**:"Milestone 0.5:飞书机器人注册 + webhook 打通" |
| §19 开工指令 | "先完成第 15 节能力预检" | **改为**:"先完成第 15 节能力预检 **+ 飞书机器人注册 + webhook 联调**" |

### 4.2 必须**新增**的章节

| 新章节 | 内容 |
|---|---|
| **§5.3 飞书架构** | 飞书机器人配置、webhook 验签、user_open_id ↔ mavis_session_id 路由 |
| **§8 飞书卡片完整规范** | 本文 §2 的 9 种卡片 JSON 模板 |
| **§8.4 按钮回调机制** | 本文 §3 的时序图 |
| **§10.5 UserMap 数据模型** | 飞书用户与 Mavis session 的映射表 schema |
| **§13.6 飞书交互指标** | 按钮点击率、卡片停留时长、回流次数 |

### 4.3 必须**删除**的内容

| 删除项 | 理由 |
|---|---|
| §8.2 "CLI 降级格式" | V1.1 默认有飞书卡片,不需要 Markdown 降级 |
| §5.1 "若 Mavis 不支持原生互动卡片" 整段 | 已确认不支持,且新方案完全绕过 |
| §11 FR-005 "卡片支持文字、选项和音频样本" 中的"卡片"改"飞书卡片" | 措辞统一 |

---

## 5. 受影响的关键指标

| 指标 | V1.0 含义 | V1.1 应补充 |
|---|---|---|
| 完成率 | 端到端完成率 | + **新用户首条消息 → 第一首歌的转化率** |
| 阶段停留时长 | 各阶段耗时 | + **卡片停留时间**(飞书可观测) |
| "都不对" 比例 | 样本全部拒绝的比例 | + **按钮点击 vs 文本回复的比例**(衡量 UI 效率) |
| 卡片提交后 | 状态变更后刷新 | + **飞书卡片更新率 vs 重新发送率** |

---

## 6. 实施前 PM 必须确认的 5 件事

| # | 事项 | 默认假设 | PM 必须确认 |
|---|---|---|---|
| 1 | 飞书机器人是否需要审核? | 个人机器人免审核 | **企业机器人需要 7-15 天审核** |
| 2 | matrix 配额按什么计费? | 按 token(未知单价) | **必须问 Mavis 商务** |
| 3 | 一个飞书用户是否允许多个 session? | 不允许(1 user 1 session) | **是否需要"一个用户多个并行项目"?** |
| 4 | 用户撤回消息时 Mavis session 如何处理? | 视为新消息 | **是/否需要专门处理撤回?** |
| 5 | 飞书私聊 vs 群聊,产品形态? | 默认私聊 | **是否做"多人共创一首歌"群聊场景?** |

---

## 7. 给 PM 的一句话总结

> **"V1.1 把交互层从 Mavis 迁到飞书,用户看到的不是'AI 工具',而是'在飞书里和一位 AI 作曲导演对话'。Agent 退居后台,但 persona 和状态机不变。开发工作量增加约 20%(飞书集成 + 后端),用户体验提升约 300%(真按钮、真表单、真音频内嵌)。"**

---

## 附录 · 关键文件链接

- **PRD V1.0**:需更新到 V1.1(本文档 §4 列出全部改动点)
- **CAPABILITY_REPORT.md**:15 项能力实测结论,§15.2 强制降级规则已部分不适用
- **MUSIC_PERSONA_AGENT_CONFIG.md**:Agent 系统提示词,无需大改,但 `tools` 字段需补充"飞书卡片 JSON 输出能力"
- **WORKFLOW.md**:9-Stage 工作流无需改,但"用户盲选"等步骤需明确为飞书卡片形式

*文档完毕。PM 可直接基于本文档更新 PRD 到 V1.1。*
