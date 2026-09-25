"""
Test Lark outbound — send a test card to user's chat.
"""
import os
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from soundmirror.tools.lark_sender import HttpLarkSender
from soundmirror.tools.contracts import load_lark_credentials_from_env

creds = load_lark_credentials_from_env()
sender = HttpLarkSender(creds)
print(f"📋 Using app_id: {creds.app_id}")
print(f"🔑 Token cache: starting fresh")

# Send to user's chat_id
chat_id = os.environ.get("USER_CHAT_ID", "")
print(f"🎯 Target chat: {chat_id}")

card = {
    "config": {"wide_screen_mode": True},
    "header": {
        "title": {"tag": "plain_text", "content": "🔔 SoundMirror / 声镜"},
        "template": "blue",
    },
    "elements": [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**Lark 端到端连通测试**\n\n"
                          "✅ Webhook 服务器已启动\n"
                          "✅ 公网 URL 已就绪\n"
                          "✅ LarkSender 已加载凭据\n\n"
                          "🌐 **Webhook URL:**\n"
                          "`https://dis-pine-gathering-began.trycloudflare.com/webhook`\n\n"
                          "📦 这张卡是从真实 Lark API 发的,不是 mock。\n\n"
                          "💡 下一步:把这个 URL 填进飞书机器人后台的 Request URL",
            },
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✓ 收到"},
                    "type": "primary",
                    "value": {"action": "ack", "test_id": "lark_e2e_01"},
                },
            ],
        },
    ],
}

result = sender.send_card(chat_id, card)
print(f"")
print(f"=== Result ===")
print(f"  message_id: {result.message_id}")
print(f"  chat_id: {result.chat_id}")
print(f"  sent_at: {result.sent_at}")
