"""
Webhook Server — Lark inbound

起一个 HTTP server,接 Lark webhook POST:
  1. /health   — GET,健康检查
  2. /webhook  — POST,Lark 事件入口

路由:
  - Lark im.message.receive_v1 (文本消息) → 走 Gateway.handle_webhook
  - Lark card.action.trigger_v1 (按钮回调) → 走 Gateway.handle_webhook

部署:
  1. cloudflared tunnel --url http://localhost:8080 → 拿到 trycloudflare URL
  2. Lark 后台 → 事件订阅 → 把 trycloudflare URL/webhook 填进 Request URL
  3. 验证 token 配对(M0C 验证流程)

安全:
  - 用 encrypt_key / verification_token 验签(M0C 验证通过)
  - actor_user_id 从 lark_open_id → product_user_id(服务端映射,不信客户端)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

# WebhookRequest 从 .gateway 引入(文件末尾的 from .gateway import 真正导入运行时类)
# 用 from __future__ import annotations 后,所有类型注解都是字符串,运行时不再求值

logger = logging.getLogger("soundmirror.webhook")

# Lark encrypt 格式(M0C 验证)
# 1. encrypt key 16字节 → AES-256-CBC
# 2. SHA256(encrypt_key) → 32 字节 key
# 3. IV = package 前 16 字节
# 4. PKCS#7 padding
#
# 这里只实现 verification_token 验证(URL 验证流程)
# encrypt 跳过 — Lark v1 不要求


def verify_lark_challenge(params: dict, verification_token: str) -> Optional[str]:
    """URL 验证:飞书回送 challenge,服务端返回明文 challenge

    用于:Lark 第一次添加 webhook URL 时验证服务端可达
    """
    challenge = params.get("challenge")
    if not challenge:
        return None
    # 不验 token,直接返回 challenge(URL 验证不签名)
    return challenge


def verify_lark_signature(
    body: bytes,
    timestamp: str,
    nonce: str,
    signature: str,
    encrypt_key: str,
) -> bool:
    """Lark v1 signature 验证(可选)

    signature = base64(SHA256(timestamp + nonce + encrypt_key + body))
    """
    if not encrypt_key:
        return True  # 没配 encrypt key 就跳过
    msg = (timestamp + nonce + encrypt_key).encode("utf-8") + body
    expected = base64.b64encode(
        hashlib.sha256(msg).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, signature)


class LarkWebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler — 路由 + 委托给 Gateway"""

    # 由调用方注入
    gateway = None
    verification_token: str = ""
    encrypt_key: str = ""

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok", "ts": time.time()})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/webhook":
            self._handle_webhook()
        elif self.path == "/webhook/url_verify":
            # URL 验证流程(Lark 后台第一次填 URL 时)
            body_bytes = self._read_body()
            try:
                params = json.loads(body_bytes)
            except json.JSONDecodeError:
                self._respond(400, {"error": "invalid json"})
                return
            challenge = verify_lark_challenge(params, self.verification_token)
            if challenge:
                self._respond(200, {"challenge": challenge})
            else:
                self._respond(400, {"error": "no challenge"})
        else:
            self._respond(404, {"error": "not found"})

    def _handle_webhook(self):
        body_bytes = self._read_body()
        if not body_bytes:
            self._respond(400, {"error": "empty body"})
            return

        # 验签(可选)
        ts = self.headers.get("X-Lark-Request-Timestamp", "")
        nonce = self.headers.get("X-Lark-Request-Nonce", "")
        sig = self.headers.get("X-Lark-Signature", "")
        if not verify_lark_signature(body_bytes, ts, nonce, sig, self.encrypt_key):
            self._respond(401, {"error": "signature invalid"})
            return

        try:
            payload = json.loads(body_bytes)
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid json"})
            return

        # 提取事件
        header = payload.get("header", {})
        event_type = header.get("event_type", "")

        if event_type == "im.message.receive_v1":
            self._handle_message_event(payload)
        elif event_type == "card.action.trigger_v1":
            self._handle_action_event(payload)
        else:
            self._respond(200, {"ok": True, "note": f"unhandled event_type: {event_type}"})

    def _handle_message_event(self, payload: dict):
        event = payload.get("event", {})
        sender = event.get("sender", {})
        message = event.get("message", {})

        # 提取 open_id / chat_id / message_id / text
        sender_open_id = sender.get("sender_id", {}).get("open_id", "")
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "")  # "p2p" / "group"
        message_id = message.get("message_id", "")
        text = self._extract_text(message.get("content", "{}"))

        # actor_user_id 用 open_id(后续 Gateway 映射到 product_user_id)
        actor_user_id = sender_open_id or f"anon_{chat_id}"

        # 路由到 Gateway
        req = WebhookRequest(
            event_id=message_id,
            user_id=actor_user_id,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            raw=payload,   # 存原始 payload,cron bridge 接管时可重建
        )
        self._enqueue_async(req)

    def _handle_action_event(self, payload: dict):
        event = payload.get("event", {})
        action = event.get("action", {})
        operator = event.get("operator", {})
        context = event.get("context", {})

        sender_open_id = operator.get("operator_id", {}).get("open_id", "")
        chat_id = context.get("open_chat_id", context.get("chat_id", ""))
        message_id = context.get("open_message_id", "")
        action_id = action.get("action_id", "")
        action_value = action.get("value", {})

        actor_user_id = sender_open_id or f"anon_{chat_id}"

        req = WebhookRequest(
            event_id=context.get("event_id", message_id),
            user_id=actor_user_id,
            chat_id=chat_id,
            message_id=message_id,
            text="",
            action_id=action_id,
            action_payload=action_value,
            raw=payload,
        )
        self._enqueue_async(req)

    # ===== fast-ACK + async worker =====
    #
    # Lark webhook 默认 3 秒超时。但 proposal.confirm 等 action 会调
    # Matrix(77-90s/路 × 3 路 = ~210s)远超过这个限制。
    #
    # 解决:
    #  1) HTTP handler 立即返回 200(< 100ms)
    #  2) 业务处理 (gateway.handle_webhook) 放到 daemon thread
    #  3) 处理完通过 LarkSender.send_card / update_card 推结果
    #
    # 重放保护:inbox_check(event_id) 在 ACK 前调用一次,在 background thread
    # 里 gateway 会再调一次,第二次必然返回 is_duplicate=True (因 PRIMARY KEY
    # 唯一约束)。background 看到 status="duplicate" 时不发任何卡(避免重复推送)。

    # 这些 action 在执行时会触发长时间外部调用,先发一张"处理中"卡给用户
    _SLOW_ACTIONS = {
        "proposal.confirm",   # 触发 3 路 Matrix 生成(~210s)
        "highlight.generate", # 重新生成(同上)
        "clip.select",        # 拉完整歌曲(~1.3s + OSS 上传)
        "highlight.select",   # 同上
    }

    def _enqueue_async(self, req: WebhookRequest):
        """立即 ACK,业务处理丢到 daemon thread

        2026-09-22 改(Bridge E2E Failure 3 - data loss bug):
          - 之前:ACK 立即返回,bg thread 启动后才 inbox_check
          - 漏洞:webhook 在 bg thread 调度前被 KILL → 事件完全丢失
          - 现在:在 ACK 前同步 inbox_check + 写 raw_payload(单 INSERT,~2ms)
            → 即使 webhook 死,DB 里至少有一行,bridge 可接管
          - 重放保护:gateway.handle_webhook 还会调一次 inbox_check,会拿到
            status='processing' → 走 gateway 内部逻辑 → 不重复发卡
          - 性能影响:ACK 多 ~2ms,远 < 3s Lark timeout
        """
        if self.gateway is None:
            self._respond(500, {"error": "gateway not configured"})
            return

        # 0) 同步 inbox_persist — 在 ACK 前持久化事件
        #    即使 webhook 立即被 KILL,事件已经在 DB 里
        #    状态 = 'received'(不抢 processing,留给 gateway.handle_webhook 接管)
        try:
            raw_payload_str = json.dumps(req.raw, ensure_ascii=False) if req.raw else None
            store = self.gateway.store if self.gateway else None
            if store is None:
                raise RuntimeError("no store available (gateway not configured)")
            inserted = store.inbox_persist(req.event_id, raw_payload_str)
            if not inserted:
                # 已存在(同一 event_id 之前收到过)→ 重放
                logger.info("duplicate event %s, no enqueue", req.event_id)
                self._respond(200, {
                    "ok": True,
                    "event_id": req.event_id,
                    "duplicate": True,
                    "ack_ts": time.time(),
                })
                return
        except Exception as e:
            logger.error("inbox_persist failed (will still ACK): %s", e)
            # 失败也 ACK — 否则 Lark 会重试到炸
            # 但 ACK 后 bridge 看不到,事件真丢
            self._respond(500, {"error": "inbox_persist failed", "detail": str(e)[:200]})
            return

        # 1) 立即 ACK(Lark 期望 < 3s)
        try:
            self._respond(200, {
                "ok": True,
                "event_id": req.event_id,
                "processing": True,
                "ack_ts": time.time(),
            })
        except Exception as e:
            logger.error("ACK failed (event in DB): %s", e)
            return

        # 2) 慢 action 先发一张"处理中"卡
        processing_msg_id = None
        if req.action_id in self._SLOW_ACTIONS and self.sender is not None and req.chat_id:
            processing_msg_id = self._send_processing_card(req)

        # 3) 业务处理丢到后台线程
        t = threading.Thread(
            target=self._process_async,
            args=(req, processing_msg_id),
            daemon=True,
            name=f"sm-worker-{req.event_id[:12] if req.event_id else 'no-eid'}",
        )
        t.start()

    def _send_processing_card(self, req: WebhookRequest) -> Optional[str]:
        """给慢 action 发一张"处理中"卡,返回 message_id 用于后续 update"""
        if self.sender is None or not req.chat_id:
            return None
        # 中文文案按 action 类型
        action_labels = {
            "proposal.confirm": ("⏳", "正在生成 3 个候选...", "约 2-3 分钟,请稍候"),
            "highlight.generate": ("⏳", "正在重新生成候选...", "请稍候"),
            "clip.select": ("⏳", "正在拉取完整歌曲...", "约 1-2 秒"),
            "highlight.select": ("⏳", "正在拉取完整歌曲...", "约 1-2 秒"),
        }
        emoji, title, sub = action_labels.get(req.action_id, ("⏳", "处理中...", "请稍候"))
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"{emoji} {title}"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{title}**\n\n{sub}\n\n"
                                  f"_event_id: `{req.event_id or '-'}`_",
                    },
                },
            ],
        }
        try:
            sent = self.sender.send_card(req.chat_id, card)
            return sent.message_id
        except Exception as e:
            logger.error("send processing card failed: %s", e)
            return None

    def _process_async(self, req: WebhookRequest, processing_msg_id: Optional[str]):
        """后台线程:跑 Gateway,完成后推结果(更新原"处理中"卡 或 发新卡)"""
        try:
            t0 = time.time()
            logger.info("[worker] start event=%s action=%s", req.event_id, req.action_id)
            resp = self.gateway.handle_webhook(req)
            dt = time.time() - t0
            logger.info("[worker] done event=%s status=%s dt=%.2fs",
                        req.event_id, resp.status, dt)

            if resp.status == "duplicate":
                # gateway 已识别为重放,不发卡
                return

            if self.sender is None:
                # 没 sender 就 dry-run,只写 log
                logger.info("[worker] no sender, would send: %s",
                            (resp.response_card or {}).get("header", {}).get("title"))
                return

            # 优先 update 已发的"处理中"卡(避免新发一张重复)
            if processing_msg_id and resp.response_card:
                try:
                    self.sender.update_card(processing_msg_id, resp.response_card)
                    logger.info("[worker] updated card %s", processing_msg_id)
                    return
                except Exception as e:
                    logger.warning("[worker] update_card failed, fall back to send: %s", e)
                    # fall through to send_card

            # 否则发新卡
            if resp.response_card:
                sent = self.sender.send_card(req.chat_id, resp.response_card)
                logger.info("[worker] sent card %s", sent.message_id)
            elif resp.text_reply:
                sent = self.sender.reply_text(req.chat_id, resp.text_reply, at_user=req.user_id)
                logger.info("[worker] sent text %s", sent.message_id)
            elif resp.status == "error":
                self._send_error_card(req, resp.error or "unknown error")

        except Exception as e:
            logger.error("[worker] exception event=%s err=%s\n%s",
                         req.event_id, e, traceback.format_exc())
            self._send_error_card(req, str(e))

    def _send_error_card(self, req: WebhookRequest, error: str):
        """业务处理异常时,推一张错误卡给用户"""
        if self.sender is None or not req.chat_id:
            return
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "❌ 处理失败"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**抱歉,处理请求时出错。**\n\n"
                                  f"**event_id**: `{req.event_id}`\n"
                                  f"**error**: `{error[:200]}`\n\n"
                                  f"请重试,或把 event_id 发给开发排查。",
                    },
                },
            ],
        }
        try:
            self.sender.send_card(req.chat_id, card)
        except Exception as e:
            logger.error("send error card failed: %s", e)

    @staticmethod
    def _extract_text(content_str: str) -> str:
        """content 是 JSON-encoded {text: '...'}"""
        try:
            obj = json.loads(content_str)
            return obj.get("text", "")
        except (json.JSONDecodeError, TypeError):
            return ""

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 5 * 1024 * 1024:  # 5MB 上限
            return b""
        return self.rfile.read(length) if length > 0 else b""

    def _respond(self, status: int, body: dict):
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def log_message(self, format, *args):
        # 不打 stderr,改打到 stdout
        print(f"[webhook] {format % args}")


# 把 WebhookRequest 引进来避免循环 import
from .gateway import WebhookRequest


def start_server(host: str = "0.0.0.0", port: int = 8080, gateway=None,
                  sender=None, verification_token: str = "",
                  encrypt_key: str = ""):
    """起一个 HTTP server,把 webhook 转给 Gateway"""
    handler = LarkWebhookHandler
    handler.gateway = gateway
    handler.sender = sender
    handler.verification_token = verification_token
    handler.encrypt_key = encrypt_key

    server = ThreadingHTTPServer((host, port), handler)
    print(f"🌐 Listening on http://{host}:{port}")
    print(f"  POST /webhook — Lark 事件入口")
    print(f"  GET  /health — 健康检查")
    print(f"  POST /webhook/url_verify — URL 验证")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Server shutting down...")
        server.shutdown()
