"""Signal Extractor — 从输入抽取意图/缺失字段/触发信号

取代老的 intent_recognition.py。输出结构化 SignalBundle。
"""
from dataclasses import dataclass, field
from typing import List, Optional

from .types import (
    InputEvent,
    SessionState,
)


@dataclass
class SignalBundle:
    """信号包:Agent Core 一轮 handle() 的输入侧摘要"""
    intent: str = "unknown"             # "fact" | "edit" | "confirm" | "select" | "ask" | "tool_result" | ...
    text_signals: List[str] = field(default_factory=list)       # 关键词命中
    tool_failures: List[str] = field(default_factory=list)      # 工具失败名
    tool_successes: List[str] = field(default_factory=list)    # 工具成功名
    detected_action: Optional[str] = None      # 从 input 推断的目标 action
    detected_payload: Optional[dict] = None    # action 的 payload

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "text_signals": self.text_signals,
            "tool_failures": self.tool_failures,
            "tool_successes": self.tool_successes,
            "detected_action": self.detected_action,
            "detected_payload": self.detected_payload,
        }


# 关键词分类(规则化,不是 LLM)
EDIT_KEYWORDS = ["改", "换", "修改", "调整", "rewrite", "改写", "重写", "edit"]
CONFIRM_KEYWORDS = ["确认", "同意", "ok", "好的", "yes", "y", "走", "继续"]
CANCEL_KEYWORDS = ["取消", "不要", "no", "n", "算了", "cancel"]
SELECT_KEYWORDS = ["选", "keep", "borrow", "reject", "a", "b", "c"]
FACT_KEYWORDS = ["今天", "昨天", "早上", "下午", "晚上", "凌晨", "中午",
                 "去年", "前年", "那年", "小时候", "第一次", "最后",
                 "味道", "声音", "气味", "触感", "画面", "感觉"]


def extract_signals(input_event: InputEvent, session: Optional[SessionState] = None) -> SignalBundle:
    """从输入事件抽取信号"""
    bundle = SignalBundle()
    text = (input_event.text or "").lower()
    bundle.text_signals = []

    # 1. 工具回调
    if input_event.kind == "tool_callback":
        bundle.intent = "tool_result"
        cb = input_event.tool_callback or {}
        if cb.get("error"):
            bundle.tool_failures.append(cb.get("tool_id", "unknown"))
        else:
            bundle.tool_successes.append(cb.get("tool_id", "unknown"))
        return bundle

    # 2. 系统恢复信号
    if input_event.kind == "resume":
        bundle.intent = "resume"
        return bundle

    # 3. 用户动作(显式)
    if input_event.kind == "action":
        bundle.intent = "action"
        bundle.detected_action = input_event.action_id
        bundle.detected_payload = input_event.action_payload
        return bundle

    # 4. 用户文本 — 分类
    if input_event.kind == "text" and text:
        # 4.1 确认/取消
        if any(kw in text for kw in CANCEL_KEYWORDS):
            bundle.intent = "cancel"
            bundle.text_signals.append("cancel_keyword")
            return bundle
        if any(kw in text for kw in CONFIRM_KEYWORDS):
            bundle.intent = "confirm"
            bundle.text_signals.append("confirm_keyword")
            return bundle
        # 4.2 编辑
        if any(kw in text for kw in EDIT_KEYWORDS):
            bundle.intent = "edit"
            bundle.text_signals.append("edit_keyword")
            return bundle
        # 4.3 选择
        if any(kw in text for kw in SELECT_KEYWORDS):
            bundle.intent = "select"
            bundle.text_signals.append("select_keyword")
            return bundle
        # 4.4 素材陈述(包含时间/感官关键词)
        if any(kw in text for kw in FACT_KEYWORDS):
            bundle.intent = "fact"
            bundle.text_signals.append("fact_keyword")
            return bundle
        # 4.5 长度 ≥ 4 字符的实质内容也当 fact
        if len(text.strip()) >= 4:
            bundle.intent = "fact"
            bundle.text_signals.append("fact_by_length")
            return bundle
        # 4.6 太短或无信号
        bundle.intent = "unclear"
        bundle.text_signals.append("unclear_short")
        return bundle

    # 5. 未知
    bundle.intent = "unknown"
    return bundle
