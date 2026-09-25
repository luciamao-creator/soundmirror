"""
LLM Client — Director V2 的 LLM 调用层

接口设计原则(V2 SPEC):
  - Director V1 用 regex + keywords 解析用户输入(deterministic)
  - Director V2 用 LLM 做语义理解(probabilistic)
  - LLM 输出必须严格按 schema(JSON),否则 fallback 到 V1

接口合约:
  - complete(system, user_messages) → str
  - 系统 prompt + 历史消息 → LLM 返回 JSON 字符串
  - mock 实现:返回 deterministic JSON(用于测试)
  - 真实实现:OpenAI / Mavis / 自定义 endpoint

V2.1 SPEC:
  - LLM 不应自行生成 action_id(白名单由 orchestrator 强制)
  - LLM 输出建议 + confidence,action_id 来自 ActionCatalog
"""
import json
from dataclasses import dataclass, field
from typing import Optional, Protocol, Any
import time
import uuid


@dataclass
class LLMMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResult:
    content: str
    model: str = "unknown"
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    raw: Optional[dict] = None


class LLMClient(Protocol):
    """LLM 抽象接口"""

    def complete(self, messages: list[LLMMessage], **kwargs) -> LLMResult: ...


class MockLLMClient:
    """Mock LLM — 用于测试,返回 deterministic 响应

    支持两种模式:
      1. 默认模式:基于用户输入关键词返回固定 JSON
      2. scripted 模式:测试可注入预设响应
    """

    def __init__(self):
        self.call_count = 0
        self.last_messages: list[LLMMessage] = []
        self.scripted_responses: list[str] = []

    def queue_response(self, content: str):
        """测试可预设下一次响应"""
        self.scripted_responses.append(content)

    def complete(self, messages: list[LLMMessage], **kwargs) -> LLMResult:
        self.call_count += 1
        self.last_messages = messages
        start = time.time()

        if self.scripted_responses:
            content = self.scripted_responses.pop(0)
        else:
            content = self._default_response(messages)

        return LLMResult(
            content=content,
            model="mock-llm-v0",
            usage={"prompt_tokens": sum(len(m.content) for m in messages) // 4,
                   "completion_tokens": len(content) // 4},
            latency_ms=int((time.time() - start) * 1000),
        )

    def _default_response(self, messages: list) -> str:
        """Mock 默认响应:从最后一条 user message 提取意图

        返回标准 envelope + state_mutation JSON
        """
        user_text = ""
        for m in reversed(messages):
            # 兼容 dict 和 LLMMessage
            if isinstance(m, dict):
                role = m.get("role")
                content = m.get("content", "")
            else:
                role = getattr(m, "role", None)
                content = getattr(m, "content", "")
            if role == "user":
                user_text = content
                break

        # 简单关键词检测
        text_lower = user_text.lower()

        # BPM
        import re
        # 尝试多种格式:"BPM 88" / "88 BPM" / "bpm 88" / "节奏 100"
        bpm_match = (
            re.search(r'(\d{2,3})\s*(?:bpm|BPM)', user_text) or
            re.search(r'(?:bpm|BPM)\s*(\d{2,3})', user_text, re.IGNORECASE) or
            re.search(r'(?:节奏|速度|拍速)\s*(\d{2,3})', user_text)
        )
        if bpm_match:
            return json.dumps({
                "envelope": {
                    "card_type": "summary",
                    "title": f"✓ 已应用:bpm={bpm_match.group(1)}",
                    "blocks": [{"tag": "div", "text": {"content": f"原话:{user_text}"}}],
                    "actions": []
                },
                "state_mutation": {
                    "expected_revision": 0,
                    "operations": [
                        {"op": "set", "path": "/music_spec/bpm",
                         "value": int(bpm_match.group(1))}
                    ]
                },
                "tool_request": None,
                "no_immediate_execution": False,
                "reasoning": f"mock LLM detected bpm={bpm_match.group(1)}"
            }, ensure_ascii=False)

        # Genre
        for genre, kws in {
            "电子": ["电子", "edm", "electronic"],
            "民谣": ["民谣", "folk", "acoustic"],
            "爵士": ["爵士", "jazz"],
            "嘻哈": ["嘻哈", "rap", "hip-hop"],
        }.items():
            if any(kw in user_text for kw in kws):
                return json.dumps({
                    "envelope": {
                        "card_type": "summary",
                        "title": f"✓ 已应用:genre={genre}",
                        "blocks": [{"tag": "div", "text": {"content": f"原话:{user_text}"}}],
                        "actions": []
                    },
                    "state_mutation": {
                        "expected_revision": 0,
                        "operations": [
                            {"op": "set", "path": "/music_spec/genre", "value": genre}
                        ]
                    },
                    "tool_request": None,
                    "no_immediate_execution": False,
                    "reasoning": f"mock LLM detected genre={genre}"
                }, ensure_ascii=False)

        # Skip / back
        if "跳过" in user_text or "下一步" in user_text:
            return json.dumps({
                "envelope": {
                    "card_type": "summary",
                    "title": "✓ 跳过当前阶段",
                    "blocks": [{"tag": "div", "text": {"content": f"你说:{user_text}"}}],
                    "actions": []
                },
                "state_mutation": None,
                "tool_request": None,
                "no_immediate_execution": False,
                "reasoning": "mock LLM detected skip"
            }, ensure_ascii=False)

        if "上一步" in user_text or "返回" in user_text:
            return json.dumps({
                "envelope": {
                    "card_type": "summary",
                    "title": "← 返回上阶段",
                    "blocks": [{"tag": "div", "text": {"content": f"你说:{user_text}"}}],
                    "actions": []
                },
                "state_mutation": None,
                "tool_request": None,
                "no_immediate_execution": False,
                "reasoning": "mock LLM detected back"
            }, ensure_ascii=False)

        # Default: 不理解
        return json.dumps({
            "envelope": {
                "card_type": "info",
                "title": "我没完全理解",
                "blocks": [
                    {"tag": "div", "text": {"content": f"你说:{user_text[:100]}"}},
                    {"tag": "note", "content": "可以说:BPM 88 / 加钢琴 / 副歌改 / 跳过"},
                ],
                "actions": []
            },
            "state_mutation": None,
            "tool_request": None,
            "no_immediate_execution": False,
            "reasoning": "mock LLM default fallback"
        }, ensure_ascii=False)


class MavisLLMClient:
    """Mavis session 外部 LLM 接口(占位 — 需要 mavis external API 文档)

    用法:
      from mavis import MavisClient  # 假设存在
      client = MavisClient(session="cs_xxx")
      llm = MavisLLMClient(client)
    """

    def __init__(self, mavis_session=None, model: str = "claude-3.5-sonnet"):
        self.mavis_session = mavis_session
        self.model = model

    def complete(self, messages: list[LLMMessage], **kwargs) -> LLMResult:
        """
        TODO: 接 Mavis external session API 后实现

        预期调用形式:
          response = self.mavis_session.llm.chat(
              messages=[{"role": m.role, "content": m.content} for m in messages],
              model=self.model,
              temperature=kwargs.get("temperature", 0.7),
              response_format={"type": "json_object"},
          )
        """
        raise NotImplementedError(
            "Mavis external API not yet integrated. "
            "需要从 mavis session 外面调 LLM。"
        )


class OpenAICompatibleLLMClient:
    """任何 OpenAI 兼容的 endpoint(OpenAI / Anthropic via proxy / vLLM / Ollama)

    配置:
      LLM_ENDPOINT=https://api.openai.com/v1/chat/completions
      LLM_API_KEY=sk-xxx
    """

    def __init__(self, endpoint: str, api_key: str, model: str = "gpt-4o-mini"):
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model

    def complete(self, messages: list[LLMMessage], **kwargs) -> LLMResult:
        import urllib.request
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": kwargs.get("temperature", 0.7),
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        start = time.time()
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        return LLMResult(
            content=content,
            model=data.get("model", self.model),
            usage=data.get("usage", {}),
            latency_ms=int((time.time() - start) * 1000),
            raw=data,
        )
