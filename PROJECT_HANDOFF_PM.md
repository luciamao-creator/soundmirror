# SoundMirror / 声镜 — Lark 上线协作文档(给 PM)

> 作者:Mavis(给 PM 看的状态 + 决策项)
> 日期:2026-09-20
> 项目根目录:`/workspace/voice-persona-assessment/`

---

## TL;DR(PM 先看这段)

SoundMirror MVP 的核心代码、测试、Matrix 真实音乐生成,**全部跑通**(202 个测试通过)。

**目前唯一卡点是:Lark webhook 公网 URL 不稳定**。沙箱进程死掉后 URL 就失效,飞书机器人后台填的 Request URL 也会跟着废。

需要 PM 帮做 **1 件事**:
**给 Mavis 1 个绑到 Cloudflare 的域名 + 1 个 Cloudflare API Token**。Mavis 用它配一个"永久的"Webhook URL,飞书机器人后台填一次就永久生效。

整套下来 ~20 分钟。

---

## 1. 项目是什么

**SoundMirror / 声镜** = 一个"个人风格音乐 Agent"。
- 用户用一段文字描述一段记忆 / 场景 / 情绪
- Agent 生成 1 首中文 indie folk 风格的歌(30s 试听 → 完整版)
- 通过飞书消息卡片交互(用户点按钮推进流程)

**架构**(已经是最终形态):
```
飞书(Lark 卡片 + 消息)
  ↕ webhook + tenant_access_token
Mavis Agent(决策 / 编排)
  ↕ Python SDK
Matrix(Mavis 内部 connector,生成真实中文 AI 歌曲)
```

**MVP 跑通的部分**(M1 prototype,全部完成):
- ✅ 状态机 + 12 个 stage handler
- ✅ 36 个 action 编排
- ✅ Intent 两层路由(regex + LLM fallback)
- ✅ Lark outbound(发卡到飞书)
- ✅ Lark inbound(webhook server,签名验证)
- ✅ Music pipeline(Real Matrix + fallback + 完整歌曲生成)
- ✅ 159 + Day1(+22) + Day2-3(+13) + Day4(+8) = **202 个测试通过**,0 回归
- ✅ 18 个 Golden Cases + 10 个 journey fixtures
- ✅ 真实 Matrix 端到端跑通:用同 case 重跑,3 个中文 AI 试听 + 完整歌曲生成成功

---

## 2. 当前状态(精确到文件)

### 2.1 已就绪的代码
```
/workspace/voice-persona-assessment/agent/soundmirror/
├── gateway/
│   ├── webhook_server.py    # Lark webhook 入口(HMAC-SHA256 验签、URL 验证、事件分发)
│   ├── gateway.py           # 编排入口(状态机 + Director + Stage + Card)
│   └── card_renderer.py     # Lark Card v1 渲染(plain_text / lark_md)
├── tools/
│   ├── lark_sender.py       # HttpLarkSender(真发卡)+ MockLarkSender
│   ├── matrix_adapter.py    # MatrixMusicAdapter(真生成)+ MockMusicAdapter
│   ├── music_pipeline.py    # MusicPipeline(real + fallback + 完整歌曲)
│   ├── audio_clipper.py     # FfmpegAudioClipper(curl + tmp + ffmpeg 模式)
│   └── asset_uploader.py    # MatrixAssetUploader(1h signed URL)
├── director/                # Director + DirectorV2 + LLM client
├── orchestrator/            # catalog(36 action)+ 状态机
├── stage/                   # 12 个 stage handler
├── storage/                 # SQLite + StateMutation 乐观锁 + audit
├── run_webhook.py           # 启动 webhook server 的 entry
├── start_lark_webhook.sh    # 启停脚本
└── tests/unit/              # 202 个 pytest

.env                         # 含 LARK_APP_ID/SECRET/USER_CHAT_ID/OPEN_ID + NGROK token(已无效)
```

### 2.2 已验证的能力(端到端)
- ✅ **真实 Lark 消息发送**:刚给 PM 发了测试卡,message_id = `om_x100b65c88d12a4acc4e6d2efb05ce57`,PM 在飞书里能看到
- ✅ **真实 Matrix 音乐生成**:`connector call batch_text_to_music` 在沙箱里跑得通,77-90s 出中文 AI 歌
- ✅ **完整 9 步 stage 流转**:从用户输入 → 3 个候选 → 选 A → 生成完整歌曲(69.67s)→ CONFIRMED
- ✅ **AIGC 法律合规**:所有 Matrix 生成的 MP3 带 ID3v2.4 AIGC label=1 tag

### 2.3 Lark 上线卡在公网 URL

**当前临时 URL**:`https://dis-pine-gathering-began.trycloudflare.com`
**问题**:
1. cloudflared 用的是 "account-less 快速 tunnel",Cloudflare 自己说"no uptime guarantee"
2. 沙箱进程死了之后,cloudflared 进程也死,**URL 失效**
3. 飞书后台如果填这个 URL,等沙箱重启,URL 变了,后台的填值就废了

**实际验证**:刚才 webhook 进程已经死了。

---

## 3. PM 需要决策的:4 个长期 URL 方案

Mavis 已经实测过沙箱环境,排除 ngrok(`tls: failed to verify certificate`,x509 拦截)。

| 方案 | 成本 | 稳定性 | PM 配合成本 | Mavis 工作量 | 推荐度 |
|------|------|--------|------------|-------------|--------|
| **A. Cloudflare 命名 tunnel + 域名** | 域名几十块/年 | 永久 | 买域名 + 改 NS + 拿 Token(~10 分钟) | ~5 分钟配 tunnel | ⭐⭐⭐⭐⭐ |
| B. ngrok 付费版 | $8/月 | 固定子域名 | 付费 + Token | ~5 分钟 | ❌ 沙箱跑不了 |
| C. Render / Fly.io 部署 | 免费 | 固定,但免费层有冷启动 | 给 Render 账号 | ~30 分钟打包 Docker | ⭐⭐ |
| D. PM 本机跑 webhook + ngrok | 免费 | 取决于 PM 电脑 | PM 一直开机 | 给 PM 写脚本 | ⭐ |

### 推荐 A 方案的具体步骤

**PM 操作(10 分钟)**:
1. 买一个便宜域名(几十块就行,如 `soundmirror.xyz` / `getsm.fun` / `mysound.fun`)
2. 把域名的 NS 记录改成 Cloudflare 的(`xxx.ns.cloudflare.com`,域名注册商后台改)
3. 注册 Cloudflare 账号 → 创建 API Token:
   - 权限:`Zone:DNS:Edit` + `Account:Cloudflare Tunnel:Edit`
   - Zone Resources:Include → Specific zone → 选你的域名
4. 把以下 3 个东西给 Mavis:
   - 域名(如 `soundmirror.xyz`)
   - Cloudflare Account ID(账号主页右边)
   - Cloudflare API Token

**Mavis 操作(5 分钟)**:
1. 用 API Token 创建 named tunnel(`soundmirror-prod`)
2. 配 DNS 路由:`soundmirror.你的域名.com` → tunnel
3. 改 `start_lark_webhook.sh` 用 named tunnel
4. 给 PM 一个永久 URL

**PM 操作(2 分钟)**:
1. 去飞书开发者后台 → 你的 app → 事件订阅
2. Request URL 填 `https://soundmirror.你的域名.com/webhook`
3. 点保存(飞书会 POST 一个 url_verification 挑战,Mavis 已经准备好验证逻辑)

**总共 ~20 分钟搞定。**

---

## 4. 备选:如果 PM 现在不想买域名

那就只能:
- **继续用临时 URL**:`dis-pine-gathering-began.trycloudflare.com`
- 沙箱进程死了 PM 让我重启就行(每次 ~3 分钟)
- 飞书后台每次换 URL 都要改一次(2 分钟)
- 适合"先看效果再决定"

或者用 **C. Render 免费部署**,但要接受:
- 15 分钟无活动后 sleep
- 首次 webhook 延迟 30-60 秒(冷启动)
- 调试体验差(每次改代码要重新部署)

---

## 5. 沙箱环境已知限制(Mavis 视角)

| 限制 | 影响 | Mavis 现在的应对 |
|------|------|----------------|
| 进程会被空闲清理 | webhook/cloudflared 会死 | 用 `start_lark_webhook.sh` 启停 |
| ngrok 被 TLS 拦截 | ngrok 用不了 | 用 cloudflared |
| Matrix 真实生成 77-90s | 一首歌要 1.5 分钟 | 在代码里用 `timeout=180s` |
| 沙箱 DNS 不稳 | `trycloudflare.com` 偶尔解析失败 | 在测试脚本里 `--resolve` 显式 IP |
| 用户已经知道的安全问题 | `.env` 里 LARK_APP_SECRET + NGROK_TOKEN 已泄漏 | 等 PM 通知做密钥轮换 |

---

## 6. PM 看完本文档,需要回答 1 个问题

> **Lark 上线 URL 用哪个方案?**
>
> - [ ] **A. 我去搞域名 + Cloudflare Token,给你之后 5 分钟就上线**
> - [ ] **先用临时 URL 凑合看效果**
> - [ ] **C. Render 部署(能接受冷启动延迟)**
> - [ ] **其他方案(你来提)**

---

## 7. 上线后立刻能跑的场景

PM 拿到 URL 之后,Mavis 立刻能跑这套端到端 demo:

1. PM 在飞书里给机器人发一段文字,例如:
   > "我想做一首关于'长大后重新回到小时候住过的海边'的歌"
2. 机器人回一张欢迎卡(蓝色)
3. PM 接着发一段素材,机器人回"已记录(1 段素材)"
4. PM 点 "够了 → 进入人格测试",机器人回 3 个候选人 A/B/C 卡片
5. PM 听试听 + 选 A,机器人回当前方案(BPM/Key/Genre/歌词)
6. PM 接受方案,机器人调用 **真实 Matrix** 生成 3 个 30s 中文 AI 歌曲(~80 秒)
7. PM 选最喜欢的一个(假设 A),机器人**自动生成完整歌曲**(70s 左右)
8. PM 听完整歌曲,回 "整体满意",机器人回"已应用:approve_review"
9. 全程 ~3 分钟,真实 AI 中文歌

如果 PM 想测 LLM 决策 / 多轮修订 / 跨 session 持久化,这套 demo 都能覆盖。

---

## 8. 已知产品级 TODO(非阻塞,先记着)

- 跨 session persona 继承(PM Acceptance A6 P2)
- 歌词敏感词过滤(PM Acceptance C6 P2)
- 修订次数预算强制(PM Acceptance B6 P2)
- 完整歌曲失败时降级方案
- Lark Card 在移动端的字体适配

这些都可以等 Lark 上线之后再说,不阻塞 PM 决策。

---

## 联系

- **谁来对接**:Mavis
- **对接方式**:直接在这条链上继续
- **建议优先级**:PM 看完 → 选 A/B/C/D → 告诉 Mavis → 20 分钟搞定

