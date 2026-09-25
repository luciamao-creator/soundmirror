"""
Director — V1.2 加入 Intent 路由层 + 全 11 stage handlers

V1.2 改进:
  1. 先跑 Intent Recognition,识别用户意图 → 直接发 action
  2. 11 个 stage handler 都实现
  3. 两阶段确认协议在 Director 层(走 confirm 卡)
"""
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .intent_recognition import detect_intent, intent_to_action


@dataclass
class AgentResult:
    envelope: dict
    state_mutation: Optional[dict] = None
    tool_request: Optional[dict] = None
    no_immediate_execution: bool = False
    trace_id: str = field(default_factory=lambda: f"tr_{uuid.uuid4().hex[:12]}")
    reasoning: str = ""


HINT_TIP = "越具体越好,比如 '我看《完美的日子》时的那种安静的孤独感' > '我喜欢安静'"


class Director:
    """deterministic + intent routing stub。LLM 替换只改这一处。"""

    def __init__(self, orchestrator=None, store=None):
        self.orchestrator = orchestrator
        self.store = store

    def process_message(
        self,
        text: str,
        actor_user_id: str,
        creative_session_id: str,
    ) -> AgentResult:
        text = text.strip()
        session = self.store.get_session(creative_session_id)
        if not session:
            return AgentResult(
                envelope={
                    "card_type": "error",
                    "title": "会话未找到",
                    "blocks": [{"tag": "div", "text": {"content": "先创建会话再说话。"}}],
                    "actions": [],
                },
                reasoning="session_not_found",
            )

        # Layer 1: Intent Recognition(在任何 stage 前)
        intent = detect_intent(text)
        if intent and intent.name in (
            "bpm_change", "key_change", "genre_change", "instrument_add",
            "skip_stage", "back_stage", "approve_review", "revise_request",
        ):
            action = intent_to_action(
                intent,
                current_revision=session["state_revision"],
                current_music_spec=session["ledger"].get("music_spec", {}),
            )
            if action:
                # 触发 orchestrator
                result = self.orchestrator.dispatch(
                    action_id=action["action_id"],
                    payload=action["payload"],
                    actor_user_id=actor_user_id,
                    creative_session_id=creative_session_id,
                )
                return self._build_from_orchestrator_result(result, intent, text)

        # Layer 2: lyric_edit / confirm_yes / reject_cancel — 这些依赖 stage context
        # 先按 stage 路由,然后 stage 内部会处理 intent

        current_stage = session["ledger"].get("current_stage", "ARCHAEOLOGY")
        handler = self._stage_handlers.get(current_stage, self._handle_unknown_stage)
        return handler(self, text, session, actor_user_id, intent)

    # =========================================================
    #  Helpers
    # =========================================================

    def _build_from_orchestrator_result(self, result, intent, original_text):
        """把 orchestrator 结果转 AgentResult"""
        if result.status == "executed":
            # 直接成功:返回 summary 卡
            return AgentResult(
                envelope={
                    "card_type": "summary",
                    "title": f"✓ 已应用:{intent.name}",
                    "blocks": [
                        {"tag": "div", "text": {"content": f"意图:`{intent.name}` 已执行"}},
                        {"tag": "note", "content": f"原话:{original_text}"},
                    ],
                    "actions": [],
                },
                reasoning=f"intent:{intent.name} executed",
            )

        if result.status == "pending":
            # 高危动作:返回 confirm 卡
            consequence = result.consequence or "这个操作不可回退"
            cost = result.cost_display or ""
            envelope = {
                "card_type": "confirmation",
                "title": "确认这个操作?",
                "header_template": "yellow",
                "blocks": [
                    {"tag": "div", "text": {"content": consequence}},
                    {"tag": "note", "content": cost},
                    {"tag": "note", "content": "⚠️ 这不可回退"},
                ],
                "actions": [
                    {"action_id": "confirm.accept", "label": "确认执行",
                     "value": {"pending_action_id": result.pending_action_id}, "danger": True},
                    {"action_id": "confirm.cancel", "label": "取消",
                     "value": {"pending_action_id": result.pending_action_id}},
                ],
            }
            return AgentResult(
                envelope=envelope,
                no_immediate_execution=True,
                reasoning=f"intent:{intent.name} pending_confirmation",
            )

        if result.status == "rejected":
            return AgentResult(
                envelope={
                    "card_type": "error",
                    "title": "操作被拒绝",
                    "header_template": "red",
                    "blocks": [{"tag": "div", "text": {"content": result.reason or "原因不明"}}],
                    "actions": [],
                },
                reasoning=f"intent:{intent.name} rejected: {result.reason}",
            )

        return AgentResult(
            envelope={"card_type": "error", "title": "未知状态",
                     "blocks": [{"tag": "div", "text": {"content": str(result)}}]},
        )

    # =========================================================
    #  Stage Handlers
    # =========================================================

    def _handle_unknown_stage(self, text, session, actor_user_id, intent):
        return AgentResult(
            envelope={
                "card_type": "error",
                "title": "未知阶段",
                "blocks": [{"tag": "div", "text": {"content": f"阶段 {session['ledger'].get('current_stage')} 未实现"}}],
            },
            reasoning="unknown_stage",
        )

    def _handle_new(self, text, session, actor_user_id, intent):
        """NEW 阶段处理

        行为分支(2026-09-18 修):
          - 用户给实质内容(>=2 字符 且 不是纯问候语)→ 直接当 archaeology_fact 收录,跳过欢迎卡
          - 用户没给文本 OR 是纯问候语("hi" / "你好" / "hello")→ 显示欢迎卡

        背景:Lark 平台下,用户第一次进入 chat 界面时会触发 chat_member_join 事件,
        该事件不携带 user_text,只用来渲染欢迎卡。REPL / 其他接入方式下没有这个事件,
        因此当用户在 NEW stage 直接打字时,应当被当作 archaeology_fact 处理,而不是被
        引导重新打一遍。
        保留对 "hi"/"hello"/"你好" 等短问候的兼容。
        """
        text_stripped = (text or "").strip()

        # 短问候语白名单:仍走欢迎卡(避免误伤旧行为)
        GREETINGS = {"hi", "hello", "你好", "hey", "嗨", "在吗", "hi~", "你好啊", "您好"}
        is_greeting = text_stripped.lower() in {g.lower() for g in GREETINGS}

        # 情况 A:用户给实质内容(非问候语 + >=2 字符)→ 直接收录,跳过欢迎卡
        if text_stripped and not is_greeting and len(text_stripped) >= 2:
            archaeology = session["ledger"].get("archaeology_facts", [])
            count = len(archaeology)
            return AgentResult(
                envelope={
                    "card_type": "summary",
                    "title": f"已记录({count+1} 段素材)",
                    "header_template": "blue",
                    "blocks": [
                        {"tag": "div", "text": {"content": f"你说:{text_stripped[:80]}"}},
                        {"tag": "note", "content": "还可以再多告诉我一些不同维度的素材,或者往下走。"},
                    ],
                    "actions": [
                        {"action_id": "archaeology.add", "label": "➕ 再加一段", "value": {}},
                        {"action_id": "archaeology.confirm", "label": f"够了 → 进入人格测试({count+1} 段)", "value": {}},
                    ],
                },
                state_mutation={
                    "expected_revision": session["state_revision"],
                    "operations": [
                        {"op": "append", "path": "/archaeology_facts",
                         "item": {"text": text_stripped, "ts": time.time()}},
                        {"op": "set", "path": "/current_stage", "value": "ARCHAEOLOGY"},
                    ],
                },
                reasoning="first_user_message_direct_record",
            )

        # 情况 B:短问候语 OR 无文本 → 显示欢迎卡
        welcome = "我会从你过去的喜好里提炼音乐人格,然后一起写歌。"
        prompt_q = "先用几句话告诉我,你最近被什么打动过? (人 / 场景 / 一首歌 / 一段情绪)"
        return AgentResult(
            envelope={
                "card_type": "onboarding",
                "title": "👋 我是 SoundMirror",
                "blocks": [
                    {"tag": "div", "text": {"content": welcome}},
                    {"tag": "div", "text": {"content": prompt_q}},
                    {"tag": "note", "content": HINT_TIP},
                ],
                "actions": [
                    {"action_id": "session.start", "label": "开始聊聊", "value": {}},
                ],
            },
            state_mutation={
                "expected_revision": session["state_revision"],
                "operations": [{"op": "set", "path": "/current_stage", "value": "ARCHAEOLOGY"}],
            },
            reasoning="onboarding_prompt",
        )

    def _handle_archaeology(self, text, session, actor_user_id, intent):
        archaeology = session["ledger"].get("archaeology_facts", [])
        count = len(archaeology)

        if text:
            return AgentResult(
                envelope={
                    "card_type": "summary",
                    "title": f"已记录({count+1} 段素材)",
                    "blocks": [
                        {"tag": "div", "text": {"content": f"你说:{text[:80]}"}},
                        {"tag": "note", "content": "还可以再多告诉我一些不同维度的素材,或者往下走。"},
                    ],
                    "actions": [
                        {"action_id": "archaeology.add", "label": "➕ 再加一段", "value": {}},
                        {"action_id": "archaeology.confirm", "label": f"够了 → 进入人格测试({count+1} 段)", "value": {}},
                    ],
                },
                state_mutation={
                    "expected_revision": session["state_revision"],
                    "operations": [
                        {"op": "append", "path": "/archaeology_facts",
                         "item": {"text": text, "ts": time.time()}},
                    ],
                },
                reasoning="archaeology_add",
            )

        actions = [{"action_id": "archaeology.add", "label": "➕ 添加素材", "value": {}}]
        if count >= 1:
            actions.append({"action_id": "archaeology.confirm", "label": "进入下一步", "value": {}})

        return AgentResult(
            envelope={
                "card_type": "info",
                "title": "考古阶段",
                "blocks": [
                    {"tag": "div", "text": {"content": f"当前有 {count} 段素材。"}},
                    {"tag": "note", "content": "再发一段文本描述你的喜好,或点击下方继续。"},
                ],
                "actions": actions,
            },
            reasoning="archaeology_status",
        )

    def _handle_proposal_review(self, text, session, actor_user_id, intent):
        """PROPOSAL_REVIEW: 用户看 proposal,可以 confirm / edit / discuss"""
        proposal = session["ledger"].get("current_proposal", {})
        if not proposal:
            return AgentResult(
                envelope={
                    "card_type": "info",
                    "title": "生成方案",
                    "blocks": [
                        {"tag": "div", "text": {"content": "基于身份和变量,我准备了一份方案。"}},
                    ],
                    "actions": [
                        {"action_id": "proposal.confirm", "label": "✓ 进入下一步", "value": {}},
                        {"action_id": "proposal.discuss", "label": "💬 讨论细节", "value": {}},
                    ],
                },
                reasoning="proposal_initial",
            )

        if intent and intent.name == "lyric_edit":
            return AgentResult(
                envelope={
                    "card_type": "summary",
                    "title": f"已记录歌词编辑:{intent.parameters['target']}",
                    "blocks": [
                        {"tag": "div", "text": {"content": f"你想改:`{intent.parameters['target']}`"}},
                        {"tag": "note", "content": f"原始输入:{text}"},
                    ],
                    "actions": [
                        {"action_id": "proposal.edit", "label": "✏️ 改这部分", "value": intent.parameters},
                    ],
                },
                state_mutation={
                    "expected_revision": session["state_revision"],
                    "operations": [
                        {"op": "append", "path": "/proposal_edits",
                         "item": {"target": intent.parameters["target"], "raw": text, "ts": time.time()}},
                    ],
                },
                reasoning="proposal_lyric_edit",
            )

        return AgentResult(
            envelope={
                "card_type": "summary",
                "title": "📝 当前方案",
                "blocks": [
                    {"tag": "div", "text": {"content": f"风格:{proposal.get('genre', '?')}"}},
                    {"tag": "div", "text": {"content": f"BPM:{proposal.get('bpm', '?')}"}},
                    {"tag": "note", "content": "可改歌词 / 确认 / 讨论"},
                ],
                "actions": [
                    {"action_id": "proposal.confirm", "label": "✓ 接受方案", "value": {}},
                    {"action_id": "proposal.edit", "label": "✏️ 改歌词", "value": {}},
                    {"action_id": "proposal.discuss", "label": "💬 讨论", "value": {}},
                ],
            },
            reasoning="proposal_view",
        )

    def _handle_variable_setup(self, text, session, actor_user_id, intent):
        """VARIABLE_SETUP: 用户改 bpm/key/genre/instrument 等"""
        music_spec = session["ledger"].get("music_spec", {})

        if intent and intent.name in ("bpm_change", "key_change", "genre_change", "instrument_add"):
            param_name = intent.parameters.get("name", intent.name.split("_")[0])
            param_value = intent.parameters.get("value") or intent.parameters.get("bpm")
            if intent.name == "key_change":
                param_value = intent.parameters["raw"]
            elif intent.name == "genre_change":
                param_value = intent.parameters["genre"]
            elif intent.name == "instrument_add":
                param_value = intent.parameters["instrument"]

            # 显示当前 + 新的
            current_value = music_spec.get(param_name, "未设")

            return AgentResult(
                envelope={
                    "card_type": "summary",
                    "title": f"✓ {param_name}: {param_value}",
                    "header_template": "blue",
                    "blocks": [
                        {"tag": "div", "text": {"content": f"`{param_name}` 从 `{current_value}` → `{param_value}`"}},
                        {"tag": "note", "content": f"原话:{text}"},
                    ],
                    "actions": [
                        {"action_id": "variable.confirm", "label": "✓ 全部确定 → 开始生成", "value": {}},
                        {"action_id": "variable.sample", "label": "🎧 先试听 30s 样本", "value": {}},
                        {"action_id": "variable.change", "label": "再改一个变量", "value": {}},
                    ],
                },
                state_mutation={
                    "expected_revision": session["state_revision"],
                    "operations": [
                        {"op": "set", "path": f"/music_spec/{param_name}", "value": param_value},
                    ],
                },
                reasoning=f"variable_change:{param_name}={param_value}",
            )

        # 无变更意图 → 列当前值
        return AgentResult(
            envelope={
                "card_type": "info",
                "title": "变量设置",
                "header_template": "blue",
                "blocks": [
                    {"tag": "div", "text": {"content": f"当前:BPM={music_spec.get('bpm','?')} key={music_spec.get('key','?')} genre={music_spec.get('genre','?')}"}},
                    {"tag": "note", "content": "可改:`BPM 88` / `降 B 大调` / `换成电子` / `加钢琴`"},
                ],
                "actions": [
                    {"action_id": "variable.change", "label": "✏️ 改变量", "value": {}},
                    {"action_id": "variable.sample", "label": "🎧 试听 30s 样本", "value": {}},
                    {"action_id": "variable.confirm", "label": "✓ 确定 → 开始生成", "value": {}},
                ],
            },
            reasoning="variable_setup_view",
        )

    def _handle_highlight_review(self, text, session, actor_user_id, intent):
        """HIGHLIGHT_REVIEW: 用户听 30s 候选高光,选最喜欢的"""
        clips = session["ledger"].get("highlight_clips", [])
        if not clips:
            return AgentResult(
                envelope={
                    "card_type": "summary",
                    "title": "生成 3 个 30s 高光",
                    "blocks": [
                        {"tag": "div", "text": {"content": "我在等待 matrix 生成 30s 候选..."}},
                    ],
                    "actions": [
                        {"action_id": "clip.select", "label": "🎧 选 A", "value": {"slot": "A"}},
                        {"action_id": "clip.select", "label": "🎧 选 B", "value": {"slot": "B"}},
                        {"action_id": "clip.select", "label": "🎧 选 C", "value": {"slot": "C"}},
                    ],
                },
                reasoning="highlight_generating",
            )

        return AgentResult(
            envelope={
                "card_type": "selection",
                "title": "选你最喜欢的高光",
                "header_template": "purple",
                "blocks": [
                    {"tag": "div", "text": {"content": "基于你的人格,A/B/C 是不同方向的 30s"}},
                ],
                "actions": [
                    {"action_id": "clip.select", "label": "✓ A", "value": {"slot": "A"}},
                    {"action_id": "clip.select", "label": "✓ B", "value": {"slot": "B"}},
                    {"action_id": "clip.select", "label": "✓ C", "value": {"slot": "C"}},
                    {"action_id": "clip.none", "label": "✗ 都不喜欢", "value": {}},
                    {"action_id": "clip.feedback", "label": "💬 给反馈", "value": {}},
                ],
            },
            reasoning="highlight_review",
        )

    def _handle_full_review(self, text, session, actor_user_id, intent):
        """FULL_REVIEW: 整首完成,等用户给反馈"""
        if intent and intent.name in ("revise_request", "approve_review"):
            # 已被 intent 层处理过,这里只 fallback
            pass

        if not text.strip():
            budget_total = session["ledger"].get("revision_budget_total", 3)
            budget_used = session["ledger"].get("revision_budget_used", 0)
            budget_remaining = budget_total - budget_used
            return AgentResult(
                envelope={
                    "card_type": "summary",
                    "title": "整首完成",
                    "header_template": "purple",
                    "blocks": [
                        {"tag": "div", "text": {"content": "想改哪里?比如:'副歌加 hook' / 'BPM 太慢' / '桥段加吉他'"}},
                        {"tag": "note", "content": f"剩余修订预算:{budget_remaining} / {budget_total}"},
                    ],
                    "actions": [
                        {"action_id": "review.approve", "label": "✓ 整体满意", "value": {}},
                        {"action_id": "revision.request", "label": "✏️ 重新修改", "value": {}},
                    ],
                },
                reasoning="full_review_prompt",
            )

        return AgentResult(
            envelope={
                "card_type": "confirmation",
                "title": "修订确认",
                "header_template": "yellow",
                "blocks": [
                    {"tag": "div", "text": {"content": f"你说:{text}"}},
                    {"tag": "div", "text": {"content": "这会消耗 1 个修订单元,Matrix 整首重新生成。"}},
                    {"tag": "note", "content": "⚠️ 整首重生成,不是 partial regen。"},
                ],
                "actions": [
                    {"action_id": "confirm.accept", "label": "确认修订", "value": {"feedback": text}},
                    {"action_id": "confirm.cancel", "label": "取消", "value": {}},
                ],
            },
            no_immediate_execution=True,
            reasoning="revision_confirmation_pending",
        )

    def _handle_revision_generating(self, text, session, actor_user_id, intent):
        """REVISION_GENERATING: 整首重生成中"""
        return AgentResult(
            envelope={
                "card_type": "summary",
                "title": "🔄 整首重新生成中",
                "header_template": "blue",
                "blocks": [
                    {"tag": "div", "text": {"content": "Matrix 正在生成修订版..."}},
                    {"tag": "note", "content": "预计 30-90 秒,完成后自动通知你"},
                ],
                "actions": [],
            },
            reasoning="revision_in_progress",
        )

    def _handle_full_reveal(self, text, session, actor_user_id, intent):
        """FULL_REVEAL: 展示完整歌曲 + 学习提议"""
        return AgentResult(
            envelope={
                "card_type": "summary",
                "title": "🎵 完整歌曲",
                "header_template": "purple",
                "blocks": [
                    {"tag": "div", "text": {"content": "你的人格和这首完整歌曲已成型。"}},
                    {"tag": "note", "content": "可以确认 / 提议学习 / 看完整归档"},
                ],
                "actions": [
                    {"action_id": "review.approve", "label": "✓ 确认归档", "value": {}},
                    {"action_id": "learning.propose", "label": "📚 提议学习这个模式", "value": {}},
                    {"action_id": "summary.request", "label": "📦 查看完整作品", "value": {}},
                ],
            },
            state_mutation={
                "expected_revision": session["state_revision"],
                "operations": [{"op": "set", "path": "/current_stage", "value": "CONFIRMED"}],
            },
            reasoning="full_reveal_prompt",
        )

    def _handle_confirmed(self, text, session, actor_user_id, intent):
        return AgentResult(
            envelope={
                "card_type": "summary",
                "title": "🎉 作品完成",
                "blocks": [
                    {"tag": "div", "text": {"content": "你的人格已沉淀。可以:1) 写新歌 2) 看作品归档"}},
                ],
                "actions": [
                    {"action_id": "session.start", "label": "🚗 写新歌", "value": {}},
                    {"action_id": "summary.request", "label": "📦 查看作品", "value": {}},
                    {"action_id": "learning.propose", "label": "📚 提一个学习提议", "value": {}},
                ],
            },
            reasoning="confirmed_status",
        )

    def _handle_profile_archived(self, text, session, actor_user_id, intent):
        """PROFILE_ARCHIVED: 归档状态"""
        return AgentResult(
            envelope={
                "card_type": "summary",
                "title": "📦 已归档",
                "header_template": "grey",
                "blocks": [
                    {"tag": "div", "text": {"content": "你的作品已存入个人资料。"}},
                ],
                "actions": [
                    {"action_id": "session.start", "label": "🚗 写新歌", "value": {}},
                ],
            },
            reasoning="profile_archived",
        )

    def _handle_identity_fitting(self, text, session, actor_user_id, intent):
        """IDENTITY_FITTING"""
        candidates = session["ledger"].get("identity_candidates", [])
        if not candidates:
            return AgentResult(
                envelope={
                    "card_type": "info",
                    "title": "计算身份候选",
                    "blocks": [
                        {"tag": "div", "text": {"content": "我从你给的素材里提炼了 3 个身份方向,请听。"}},
                    ],
                    "actions": [
                        {"action_id": "identity.play", "label": "试听 A 贴耳", "value": {"slot": "A"}},
                        {"action_id": "identity.play", "label": "试听 B 自然", "value": {"slot": "B"}},
                        {"action_id": "identity.play", "label": "试听 C 辽阔", "value": {"slot": "C"}},
                    ],
                },
                reasoning="identity_fitting_start",
            )

        if text.lower() in ("保留 a", "保留 b", "保留 c", "保留a", "保留b", "保留c"):
            slot = text.lower()[-1].upper()
            return AgentResult(
                envelope={
                    "card_type": "summary",
                    "title": f"✓ 保留 {slot}",
                    "blocks": [
                        {"tag": "div", "text": {"content": f"把 {slot} 写进人格候选。"}},
                    ],
                    "actions": [
                        {"action_id": "identity.keep", "label": f"✓ 已选 {slot}", "value": {"slot": slot}},
                    ],
                },
                state_mutation={
                    "expected_revision": session["state_revision"],
                    "operations": [
                        {"op": "append", "path": "/persona_selections",
                         "item": {"action": "keep", "slot": slot, "ts": time.time()}},
                    ],
                },
                reasoning="identity_keep",
            )

        return AgentResult(
            envelope={
                "card_type": "selection",
                "title": "3 个身份候选",
                "blocks": [
                    {"tag": "div", "text": {"content": "A 贴耳 - B 自然 - C 辽阔"}},
                    {"tag": "note", "content": "点 播放 试听,或直接说 保留 A"},
                ],
                "actions": [
                    {"action_id": "identity.play", "label": "▶ A 贴耳", "value": {"slot": "A"}},
                    {"action_id": "identity.play", "label": "▶ B 自然", "value": {"slot": "B"}},
                    {"action_id": "identity.play", "label": "▶ C 辽阔", "value": {"slot": "C"}},
                    {"action_id": "identity.keep", "label": "保留 A", "value": {"slot": "A"}},
                    {"action_id": "identity.keep", "label": "保留 B", "value": {"slot": "B"}},
                    {"action_id": "identity.keep", "label": "保留 C", "value": {"slot": "C"}},
                ],
            },
            reasoning="identity_prompt",
        )

    def _handle_candidate_generating(self, text, session, actor_user_id, intent):
        """CANDIDATE_GENERATING: 候选生成中"""
        return AgentResult(
            envelope={
                "card_type": "summary",
                "title": "🎵 候选生成中",
                "header_template": "purple",
                "blocks": [
                    {"tag": "div", "text": {"content": "Matrix 在生成 5 个候选版本..."}},
                    {"tag": "note", "content": "完成后会推送给你"},
                ],
                "actions": [],
            },
            reasoning="candidate_generating",
        )

    _stage_handlers = {
        "NEW": _handle_new,
        "ARCHAEOLOGY": _handle_archaeology,
        "PROPOSAL_REVIEW": _handle_proposal_review,
        "IDENTITY_FITTING": _handle_identity_fitting,
        "VARIABLE_SETUP": _handle_variable_setup,
        "CANDIDATE_GENERATING": _handle_candidate_generating,
        "HIGHLIGHT_REVIEW": _handle_highlight_review,
        "FULL_REVIEW": _handle_full_review,
        "REVISION_GENERATING": _handle_revision_generating,
        "FULL_REVEAL": _handle_full_reveal,
        "CONFIRMED": _handle_confirmed,
        "PROFILE_ARCHIVED": _handle_profile_archived,
    }
