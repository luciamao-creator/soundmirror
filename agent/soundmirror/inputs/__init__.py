"""Input Adapter — 把外部输入(text / action)转为 InputEvent

本阶段只提供:
  CliInput          — 命令行 REPL
  ScriptedInput     — 评测脚本驱动(预先定义 sequence)

后续 Lark / Web 等接入时,新增 LarkInput / WebInput 即可,Agent Core 不变。
"""
from ..core.types import InputEvent
import time


class InputAdapter:
    """Input Adapter 抽象基类"""

    def next_event(self) -> InputEvent:
        raise NotImplementedError


class ScriptedInput(InputAdapter):
    """评测脚本驱动:预先定义 InputEvent 序列"""

    def __init__(self, events: list):
        self.events = list(events)
        self._idx = 0

    def next_event(self) -> InputEvent:
        if self._idx >= len(self.events):
            return None
        ev = self.events[self._idx]
        self._idx += 1
        if ev.occurred_at == 0:
            ev.occurred_at = time.time()
        return ev

    def has_more(self) -> bool:
        return self._idx < len(self.events)


class CliInput(InputAdapter):
    """命令行 REPL 输入"""

    def __init__(self, actor_user_id: str = "cli_user", chat_id: str = "cli_chat"):
        self.actor_user_id = actor_user_id
        self.chat_id = chat_id
        self._counter = 0

    def next_event(self) -> InputEvent:
        try:
            raw = input(">>> ")
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw.strip():
            return None

        self._counter += 1
        event_id = f"cli_{self._counter:04d}"

        # 解析前缀
        text = raw.strip()
        if text.startswith(":"):
            # :action_id[payload] 形式
            return self._parse_action(text[1:], event_id)
        elif text.startswith("/"):
            return self._parse_command(text[1:], event_id)
        else:
            return InputEvent(
                event_id=event_id,
                kind="text",
                actor_user_id=self.actor_user_id,
                chat_id=self.chat_id,
                text=text,
                occurred_at=time.time(),
            )

    def _parse_action(self, body: str, event_id: str) -> InputEvent:
        # 格式: action_id 或 action_id[slot=A] 或 action_id{k:v}
        import re
        m = re.match(r"^(\w+(?:\.\w+)?)(?:\[(.+?)\])?$", body)
        if not m:
            return InputEvent(
                event_id=event_id,
                kind="text",
                actor_user_id=self.actor_user_id,
                chat_id=self.chat_id,
                text=":" + body,
                occurred_at=time.time(),
            )
        action_id = m.group(1)
        payload_str = m.group(2) or ""
        payload = {}
        if payload_str:
            for pair in payload_str.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    v = v.strip()
                    if v.startswith("'") or v.startswith('"'):
                        v = v[1:-1]
                    payload[k.strip()] = v
        return InputEvent(
            event_id=event_id,
            kind="action",
            actor_user_id=self.actor_user_id,
            chat_id=self.chat_id,
            action_id=action_id,
            action_payload=payload,
            occurred_at=time.time(),
        )

    def _parse_command(self, body: str, event_id: str) -> InputEvent:
        # /confirm → 输入 confirm 信号
        # /cancel  → cancel
        # /quit    → stop
        cmd = body.strip().lower()
        if cmd == "confirm" or cmd == "yes" or cmd == "y":
            return InputEvent(
                event_id=event_id, kind="text",
                actor_user_id=self.actor_user_id, chat_id=self.chat_id,
                text="确认", occurred_at=time.time(),
            )
        if cmd == "cancel" or cmd == "no" or cmd == "n":
            return InputEvent(
                event_id=event_id, kind="text",
                actor_user_id=self.actor_user_id, chat_id=self.chat_id,
                text="取消", occurred_at=time.time(),
            )
        if cmd == "quit" or cmd == "exit":
            return None
        return InputEvent(
            event_id=event_id, kind="text",
            actor_user_id=self.actor_user_id, chat_id=self.chat_id,
            text=":" + body,
            occurred_at=time.time(),
        )
