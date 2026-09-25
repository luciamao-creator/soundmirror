"""Mock Lark Tool Adapter — 飞书发送 mock

仅记录,不实际发送。本阶段不需要连飞书。
"""
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .tool_adapter import (
    LarkToolAdapter,
    SendCardRequest,
    SendCardResult,
)


@dataclass
class MockLarkAdapter:
    """把卡片写入内存,提供查询接口"""

    name = "MockLarkAdapter"
    is_mock = True

    sent_cards: List[Dict[str, Any]] = field(default_factory=list)
    replied_texts: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        # 兼容 dataclass field 与实例属性的冲突
        if not hasattr(self, "sent_cards") or self.sent_cards is None:
            self.sent_cards = []
        if not hasattr(self, "replied_texts") or self.replied_texts is None:
            self.replied_texts = []

    def health(self) -> bool:
        return True

    def send_card(self, req: SendCardRequest) -> SendCardResult:
        message_id = f"mock_msg_{uuid.uuid4().hex[:12]}"
        self.sent_cards.append({
            "message_id": message_id,
            "chat_id": req.chat_id,
            "title": req.title,
            "content": req.content,
            "actions": req.actions,
            "template": req.template,
            "update_message_id": req.update_message_id,
        })
        return SendCardResult(ok=True, message_id=message_id)

    def reply_text(self, chat_id: str, text: str) -> SendCardResult:
        message_id = f"mock_msg_{uuid.uuid4().hex[:12]}"
        self.replied_texts.append({
            "message_id": message_id,
            "chat_id": chat_id,
            "text": text,
        })
        return SendCardResult(ok=True, message_id=message_id)

    def clear(self):
        self.sent_cards.clear()
        self.replied_texts.clear()


def make_default_lark_tool() -> LarkToolAdapter:
    return MockLarkAdapter()
