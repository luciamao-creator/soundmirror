# 上传到你的个人 GitHub

## 现状

代码、git 历史、证据、依赖锁、`.env.example` 都已备齐,**沙箱里没有 GitHub 凭证,无法直接 push**。

给你 3 个文件(全部在 `/workspace/`):

| 文件 | 大小 | 用途 |
|------|------|------|
| `soundmirror-export.bundle` | 420 KB | **首选** — 完整 git 历史,1 个文件搞定 |
| `soundmirror-export.zip` | 1.0 MB | 完整目录(含 .git/),双击解压即可 |
| `soundmirror-export.tar.gz` | 360 KB | 同 zip,Linux/Mac 用 |

---

## 方案 A:用 bundle(保留完整 git 历史)推荐

### 1. 在 GitHub 上创建空仓库

- 打开 https://github.com/new
- Repository name: `soundmirror`(或你喜欢的)
- **不要**勾选 Add a README / .gitignore / license(用我们提供的)
- Public 或 Private 随你
- 创建

### 2. 把 bundle clone 到本地,加 remote,push

```bash
# 假设 bundle 在 ~/Downloads
git clone ~/Downloads/soundmirror-export.bundle soundmirror
cd soundmirror

# 验证历史
git log --oneline
# 应该看到 7 个 commit,跟 git bundle verify 报告的一致

# 加 remote
git remote add origin https://github.com/<你的用户名>/soundmirror.git

# 推送
git push -u origin master
# 或 main(看你的 default branch)
```

如果 GitHub 用 PAT:
```bash
# 推送时它会要用户名和密码 → 密码那里填 PAT(不是账号密码)
git push -u origin master
# Username: <你的用户名>
# Password: <你的 PAT,ghp_xxx...>
```

SSH 党:
```bash
git remote add origin git@github.com:<你的用户名>/soundmirror.git
git push -u origin master
```

---

## 方案 B:用 zip/tarball(没有 .git 历史)

```bash
unzip soundmirror-export.zip       # 或 tar xzf soundmirror-export.tar.gz
cd soundmirror-export
git init
git add .
git commit -m "Initial commit: SoundMirror Agent Core v0

- 7-stage gated music creation agent
- 53/53 verifiable assertions
- 6 real bugs found and fixed
- Real SIGKILL recovery verified
- See README.md and CHANGELOG.md"

# 然后加 remote push
git remote add origin https://github.com/<你的用户名>/soundmirror.git
git push -u origin master
```

> ⚠️ 这样会丢 7 个 commit 的历史,只有一个 initial commit。

---

## 方案 C:脚本一键推送(最快)

```bash
cd /path/to/soundmirror-export
GITHUB_USER=你的用户名
GITHUB_PAT=ghp_你的token
GITHUB_REPO=soundmirror

git remote add origin https://${GITHUB_USER}:${GITHUB_PAT}@github.com/${GITHUB_USER}/${GITHUB_REPO}.git
git push -u origin master
```

> ⚠️ PAT 会进 shell history(临时不致命)。用完建议 `history -c` 或加空格前缀 `GH_PAT= ghp_...`。

---

## 推送后建议做这些

1. **添加 topics**:`music-generation`, `agent`, `agentcore`, `python`, `chatbot`
2. **加 description**:"Personal style music agent — 7-stage gated flow with verifiable evidence"
3. **设置 About → Releases → v0.1.0**:
   - 标题: `Agent Core v0`
   - 内容:粘贴 `CHANGELOG.md` 的 v0.1.0 section
4. **不要把 .env 上传**(已脱敏,但留意)

---

## 收仓内容清单(也写在 README)

```
soundmirror/
├── README.md                              ← 入口
├── CHANGELOG.md                           ← 版本历史
├── LICENSE                                ← MIT
├── .gitignore
├── .env.example                           ← 脱敏,真实值从飞书/Mavis 后台填
├── requirements.txt                       ← 实际只有 pytest
├── AGENTS.md / WORKFLOW.md / ...          ← 顶层设计文档
│
└── agent/soundmirror/
    ├── core/                              ← Agent Core v0
    │   ├── types.py / stage_policy.py / ask_rules.py
    │   ├── evidence.py / risk.py / signal_extractor.py
    │   ├── tool_adapter.py / music_tool_mock.py / lark_tool_mock.py
    │   ├── agent_core.py / trace.py / recovery.py
    │   └── deprecated.py
    ├── director/                          ← DirectorV2 占位
    ├── orchestrator/ / gateway/ / stage/ / tools/
    ├── storage/ / prompts/ / fixtures/ / schemas/
    ├── inputs/                            ← CliInput / ScriptedInput
    ├── tests/                             ← 213 unit + 21 eval
    ├── reports/                           ← V0 阶段报告
    └── evidence/                          ← ★ 可验证证据
        ├── verify.sh                      ← 一键复核
        ├── V0_EVIDENCE.json
        └── artifacts/audio/               ← 4 个真实音频
```

---

## 验证推送后仓库是否完整

在你 clone 下来的目录里跑:

```bash
cd agent
pip install -r ../requirements.txt      # 装 pytest
python -m pytest soundmirror/tests -q   # 213 passed
bash soundmirror/evidence/verify.sh     # 53/53 + 26/26 + 3/3
```

期望:
- `213 passed, 2 skipped`
- `53/53 断言通过`
- `独立复核: 26/26 一致`
- `变异测试: 3/3`
- `真 kill: 3/3`

任何一步失败,说明 bundle/tarball 损坏或 push 丢文件。
