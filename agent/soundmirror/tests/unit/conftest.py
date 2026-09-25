"""
Webhook server fixture — shared across test_webhook_server.py and test_webhook_fast_ack.py
"""
import json
import os
import socket
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from soundmirror.gateway import (
    LarkCardV1Renderer,
    LarkWebhookHandler,
)
from soundmirror.gateway.gateway import Gateway
from soundmirror.orchestrator import ActionCatalog
from soundmirror.orchestrator.orchestrator import Orchestrator
from soundmirror.director import Director
from soundmirror.stage import StageProcessor
from soundmirror.storage import StateStore
from soundmirror.tools import MockLarkSender


CATALOG_PATH = str(Path(__file__).parent.parent.parent / "action_catalog.json")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def server_setup():
    """起一个真 HTTP server + Gateway + MockLarkSender(模块级共享)"""
    port = _free_port()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    store = StateStore(db_path)
    catalog = ActionCatalog(CATALOG_PATH)
    orch = Orchestrator(catalog, store)
    director = Director(orchestrator=orch, store=store)
    stage_proc = StageProcessor(orch, store)
    renderer = LarkCardV1Renderer()
    sender = MockLarkSender()

    gw = Gateway(
        state_store=store,
        orchestrator=orch,
        director=director,
        stage_processor=stage_proc,
        card_renderer=renderer,
    )

    handler = LarkWebhookHandler
    handler.gateway = gw
    handler.sender = sender

    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    yield {"port": port, "sender": sender, "store": store}

    server.shutdown()
    server_thread.join(timeout=2)
    os.unlink(db_path)
