"""
Lark Sender — 实际 POST 到 Lark /im/v1/messages 飞书 API

实现 LarkSender protocol:
  - send_card(chat_id, card_json) → 发送 interactive 卡
  - update_card(message_id, card_json) → 更新已发送的卡(update_message_id)
  - reply_text(chat_id, text) → 普通文本回复

HTTP 客户端: 用内置 urllib(没有外部依赖),避免污染 requirements

合约(参考 M0C_SPIKE_REPORT):
  - POST https://open.feishu.cn/open-apis/im/v1/messages
  - Header: Authorization=Bearer {tenant_access_token}
  - Body: {receive_id, msg_type=interactive, content=JSON.stringify(card)}
  - 更新卡: PATCH /im/v1/messages/{message_id}

Tenant_access_token 获取:
  POST /auth/v3/tenant_access_token/internal
  Body: {app_id, app_secret}
  Cache 10 分钟(过期时间 2 小时)
"""
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional
from .contracts import LarkSender, LarkCredentials, SentCard


class HttpLarkSender:
    """HTTP 实现:真发卡到飞书"""

    HOST = "https://open.feishu.cn"

    def __init__(self, credentials: LarkCredentials):
        self.credentials = credentials
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # =========================================================
    #  Token Management
    # =========================================================

    def _get_tenant_access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        url = f"{self.HOST}/open-apis/auth/v3/tenant_access_token/internal"
        body = json.dumps({
            "app_id": self.credentials.app_id,
            "app_secret": self.credentials.app_secret,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("code") != 0:
            raise LarkAPIError(f"token fetch failed: {data}", code=data.get("code"))

        self._token = data["tenant_access_token"]
        self._token_expires_at = time.time() + data.get("expire", 7200) - 300  # 提前 5 分钟刷新
        return self._token

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_tenant_access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # =========================================================
    #  Public API
    # =========================================================

    def send_card(
        self,
        chat_id: str,
        card_json: dict,
        reply_to: Optional[str] = None,
    ) -> SentCard:
        """发新卡到 chat(接收者 receive_id)

        Args:
            chat_id: 飞书 chat_id (oc_xxx)
            card_json: Lark Interactive Card v1 JSON
            reply_to: 可选,作为 message parent(消息回复)
        """
        url = f"{self.HOST}/open-apis/im/v1/messages?receive_id_type=chat_id"
        body = json.dumps({
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card_json, ensure_ascii=False),
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=body, method="POST", headers=self._auth_headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise LarkAPIError(f"send_card HTTP {e.code}: {e.read()}", code=e.code)

        if data.get("code") != 0:
            raise LarkAPIError(f"send_card rejected: {data.get('msg')}", code=data.get("code"))

        return SentCard(
            message_id=data["data"]["message_id"],
            chat_id=chat_id,
        )

    def update_card(self, message_id: str, card_json: dict) -> bool:
        """更新已发送的卡(用于 echo 卡片持续更新状态)

        PATCH /im/v1/messages/{message_id}
        Body: {"content": "<json-as-string>"}
        """
        url = f"{self.HOST}/open-apis/im/v1/messages/{message_id}"
        body = json.dumps({
            "content": json.dumps(card_json, ensure_ascii=False),
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="PATCH", headers=self._auth_headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise LarkAPIError(f"update_card HTTP {e.code}: {e.read()}", code=e.code)
        return data.get("code") == 0

    def reply_text(self, chat_id: str, text: str, at_user: Optional[str] = None) -> SentCard:
        """发纯文本消息"""
        url = f"{self.HOST}/open-apis/im/v1/messages?receive_id_type=chat_id"
        text_content = text
        if at_user:
            text_content = f"<at user_id=\"{at_user}\"></at> {text_content}"
        body = json.dumps({
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text_content}),
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers=self._auth_headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise LarkAPIError(f"reply_text HTTP {e.code}: {e.read()}", code=e.code)
        if data.get("code") != 0:
            raise LarkAPIError(f"reply_text rejected: {data.get('msg')}", code=data.get("code"))
        return SentCard(
            message_id=data["data"]["message_id"],
            chat_id=chat_id,
        )


class LarkAPIError(Exception):
    def __init__(self, message: str, code: int = -1):
        self.message = message
        self.code = code
        super().__init__(message)


class MockLarkSender:
    """Memory 假发器,无网络,用于测试"""

    def __init__(self):
        self.sent = []  # [(chat_id, card, msg_id), ...]
        self.updates = []
        self.texts = []
        self._counter = 0

    def _new_id(self):
        self._counter += 1
        return f"om_mock_{self._counter:08d}"

    def send_card(self, chat_id, card_json, reply_to=None):
        msg_id = self._new_id()
        self.sent.append((chat_id, card_json, msg_id))
        return SentCard(message_id=msg_id, chat_id=chat_id)

    def update_card(self, message_id, card_json):
        self.updates.append((message_id, card_json))
        return True

    def reply_text(self, chat_id, text, at_user=None):
        self.texts.append((chat_id, text, at_user))
        return SentCard(message_id=self._new_id(), chat_id=chat_id)
