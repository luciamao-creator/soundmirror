"""Deprecated 模块的统一 import

本阶段不连飞书 / 不连真实音乐模型 / 不连公网。
但旧代码保留可用,后续 Lark / Matrix / OSS 接入时直接恢复。

这些 import 不应被新代码使用;只是确保旧模块不进入"僵尸"状态。
"""
import warnings
import importlib


DEPRECATED_MODULES = [
    ("soundmirror.run_webhook", "Lark webhook 入口,本阶段不连飞书;M3 接入时改 transport adapter"),
    ("soundmirror.start_lark_webhook", "Lark 启动脚本;同上"),
    ("soundmirror.gateway.webhook_server", "Lark HTTP handler;同上"),
    ("soundmirror.gateway.card_renderer", "飞书卡片渲染;后续 Lark 接入时直接复用"),
    ("soundmirror.tools.matrix_adapter", "真 Matrix 适配;切真音乐后端时替换 MusicToolAdapter"),
    ("soundmirror.tools.lark_sender.HttpLarkSender", "真 Lark 发送;真 Lark 接入时替换 LarkToolAdapter"),
    ("soundmirror.tools.lark_connection_test", "Lark 连通性测试;真 Lark 接入时启用"),
    ("soundmirror.worker.base", "异步 worker 队列(M2 设计);生产部署时启用"),
    ("soundmirror.worker.music_worker", "异步 music worker;同上"),
]


def _warn(name: str, msg: str):
    warnings.warn(f"[deprecated] {name}: {msg}", DeprecationWarning, stacklevel=2)


def import_deprecated(name: str, msg: str = ""):
    """按需 import 一个 deprecated 模块,带 warning"""
    _warn(name, msg or "no longer used in Agent Core v0")
    return importlib.import_module(name)
