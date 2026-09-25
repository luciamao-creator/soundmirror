"""
Card Renderer — 把 Director 输出的 envelope 转成 Lark/Feishu Interactive Card v1 JSON

约束(M0C_CARDS_SPIKE_REPORT):
  - 用 v1 schema(不是 v2,v2 反而严格)
  - 只用 button + 文字(避免 input/select_static/collapsible/checkbox 等不稳定标签)
  - 每个 button 必须有 action_id + label + value(JSON.stringify 后的 dict)

输入 envelope 格式:
  {
    "card_type": "summary" | "audio_review" | "lyrics_review" | "candidate_grid" |
                 "confirmation" | "onboarding" | "...",
    "title": "...",
    "blocks": [
      {"tag": "div", "text": {"tag": "plain_text", "content": "..."}},
      {"tag": "img", "img_key": "..."},
      {"tag": "audio", "src": "https://..."},
      ...
    ],
    "actions": [
      {"action_id": "...", "label": "...", "value": {...}},
      ...
    ],
    "header_template": "blue" | "green" | "red" | "yellow" | "indigo" | "purple",
    "update_message_id": "om_xxx"  # Optional,如果是要更新现有消息
  }

输出 Lark Card JSON:
  {
    "config": {"wide_screen_mode": true},
    "header": {...},
    "elements": [
      {"tag": "div", "text": {...}},
      {"tag": "hr"},
      {"tag": "action", "actions": [{"tag": "button", "text": {...}, "type": "primary", "value": {...}}]}
    ]
  }
"""
import json
from typing import Optional


# 卡型 → header 颜色(标准 Lark 模板枚举)
CARD_TEMPLATES = {
    "summary": "blue",
    "onboarding": "indigo",
    "audio_review": "purple",
    "lyrics_review": "blue",
    "candidate_grid": "purple",
    "confirmation": "yellow",
    "selection": "blue",
    "error": "red",
    "warning": "orange",
    "info": "grey",
}


class LarkCardV1Renderer:
    """V1 schema 实现,优先兼容性"""

    def render(self, envelope: dict, update_existing: bool = False) -> dict:
        """render envelope → Lark Card v1 JSON
        Args:
            envelope: Director 输出的 envelope
            update_existing: True 时省略 update_action(消息已发出)
        Returns:
            卡 JSON,准备好 POST 到 im/v1/messages
        """
        card_type = envelope.get("card_type", "summary")
        template = envelope.get("header_template") or CARD_TEMPLATES.get(card_type, "blue")

        elements = []

        # 1. blocks → elements
        for block in envelope.get("blocks", []):
            elements.append(self._render_block(block))

        # 2. 分隔
        if envelope.get("actions"):
            # 先加 hr
            elements.append({"tag": "hr"})

        # 3. actions → button group
        actions_list = envelope.get("actions", [])
        if actions_list:
            elements.append(self._render_action_buttons(actions_list))

        card = {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": envelope.get("title", "SoundMirror"),
                },
                "template": template,
            },
            "elements": elements,
        }

        # 高危动作的二次确认卡(payload 加 pending_action_id)
        if envelope.get("pending_action_id"):
            card["config"]["confirm_first"] = False  # 我们用按钮 instead

        return card

    def _render_block(self, block: dict) -> dict:
        """render 单个 block → Lark element"""
        tag = block.get("tag")

        if tag == "div":
            return {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": block.get("text", {}).get("content", ""),
                },
            }

        if tag == "img":
            return {
                "tag": "img",
                "img_key": block.get("img_key", ""),
                "alt": block.get("alt", ""),
            }

        if tag == "audio":
            # audio tag in v1 不太标准,用 div + href 代替
            return {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"🎵 [预览音频]({block.get('src', '')})",
                },
            }

        if tag == "field":
            # 标准 field short form
            return {
                "tag": "div",
                "fields": [{
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": block.get("text", ""),
                    },
                }],
            }

        if tag == "note":
            return {
                "tag": "note",
                "elements": [{
                    "tag": "plain_text",
                    "content": block.get("content", ""),
                }],
            }

        if tag == "hr":
            return {"tag": "hr"}

        # fallback
        return {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": str(block)[:200],
            },
        }

    def _render_action_buttons(self, actions: list[dict]) -> dict:
        """render actions → action 元素"""
        buttons = []
        for action in actions:
            buttons.append(self._render_button(action))

        return {
            "tag": "action",
            "actions": buttons,
        }

    def _render_button(self, action: dict) -> dict:
        """单个 button"""
        action_id = action.get("action_id", "")
        label = action.get("label", "按钮")
        value = action.get("value", action.get("payload", {}))

        # button type: primary(default) | danger | default
        if action.get("danger"):
            btn_type = "danger"
        elif action.get("danger") is False:
            btn_type = "default"
        else:
            # destructive action 默认 danger
            is_destructive = action.get("risk") == "destructive"
            btn_type = "danger" if is_destructive else "primary"

        return {
            "tag": "button",
            "text": {
                "tag": "plain_text",
                "content": label,
            },
            "type": btn_type,
            "value": {
                "action_id": action_id,
                **value,
            },
        }

    def render_text_response(self, text: str, at_user: Optional[str] = None) -> dict:
        """渲染纯文本消息(非卡)"""
        msg = {"text": text}
        if at_user:
            msg["text"] = f"<at user_id=\"{at_user}\"></at> {msg['text']}"
        return msg

    def render_confirmation_card(self, original_action: dict, consequence: str,
                                  cost: str = "") -> dict:
        """专门渲染高危动作二次确认卡"""
        envelope = {
            "card_type": "confirmation",
            "title": "确认这个操作?",
            "header_template": "yellow",
            "blocks": [
                {"tag": "div", "text": {"content": consequence}},
                {"tag": "note", "content": cost},
                {"tag": "note", "content": "⚠️ 这不可回退(确认后会导致上述影响)"},
            ],
            "actions": [
                {
                    "action_id": "confirm.accept",
                    "label": "确认执行",
                    "value": {"pending_action_id": original_action.get("pending_action_id", "")},
                    "danger": True,
                },
                {
                    "action_id": "confirm.cancel",
                    "label": "取消",
                    "value": {"pending_action_id": original_action.get("pending_action_id", "")},
                },
            ],
        }
        return self.render(envelope)
