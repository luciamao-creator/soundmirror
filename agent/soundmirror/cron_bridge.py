"""
Cron Bridge Daemon — 定时扫描 inbox 里的待处理事件

设计目标:
  - 即使 webhook 进程死了,这个守护进程仍能从 Lark API 直接拉消息
  - 即使守护进程死了,webhook 仍能把消息写进 inbox,守护进程重启能恢复
  - 不重复生成(走 inbox_check 的 PRIMARY KEY 唯一约束)
  - 不丢档案(state_store SQLite 持久)

实现:
  - 循环 scan inbox 表,找 status='received' 但没被处理的 event
  - 对每个 pending event,fetch 原始 Lark 消息(或用 webhook 已经存的 raw_payload)
  - 调用 gateway.handle_webhook
  - 推 response card 到 Lark
  - 标记 inbox status='processed'

定时语义:
  - sleep INTERVAL_SECONDS,默认 30s
  - 也可作为 cron 的命令启动(直接退出,无 loop),由外部 cron 触发

日志:
  - 写到 /workspace/.../out/lark_logs/bridge.log
  - 包含:扫到的 pending 数 / 处理结果 / 任何错误
"""
import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

# Setup path
HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE.parent))

# Logging
LOG_PATH = HERE / "out" / "lark_logs" / "bridge.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bridge] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bridge")


def build_gateway():
    """组装一个 Gateway 实例,共享 webhook server 的 DB"""
    from soundmirror.storage import StateStore
    from soundmirror.orchestrator import ActionCatalog
    from soundmirror.orchestrator.orchestrator import Orchestrator
    from soundmirror.director import Director
    from soundmirror.stage import StageProcessor
    from soundmirror.gateway import LarkCardV1Renderer
    from soundmirror.gateway.gateway import Gateway

    db_path = os.environ.get("SOUNDMIRROR_DB", str(HERE / "out" / "lark_webhook.db"))
    store = StateStore(db_path)
    catalog = ActionCatalog(str(HERE / "action_catalog.json"))
    # 2026-09-22 改(Bridge E2E):bridge 也需要 mutation handlers
    # 否则 webhook 死前已 inbox_check 但还没 dispatch 的 action,
    # bridge 接管后 orchestrator.handlers 是空 → stage 不推进
    from soundmirror.orchestrator.mutation_handlers import build_mutation_handlers
    handlers = build_mutation_handlers(store)
    orch = Orchestrator(catalog, store, mutation_handlers=handlers)
    director = Director(orchestrator=orch, store=store)
    stage_proc = StageProcessor(orch, store)
    renderer = LarkCardV1Renderer()

    gw = Gateway(
        state_store=store,
        orchestrator=orch,
        director=director,
        stage_processor=stage_proc,
        card_renderer=renderer,
    )
    return gw, store, db_path


def get_pending_events(store):
    """
    找所有 inbox 里 status IN ('received', 'processing') 且 raw_payload 不为空的 event_id。

    2026-09-22 改(Failure 1 cron bridge):
      - 旧版本只看 status='received',会漏掉 webhook 死时 status='processing' 的孤儿
      - webhook 的 inbox_check 把 row 原子 INSERT/UPDATE 为 'processing' 后挂掉,
        row 就卡在 'processing' 永远没人接
      - bridge 现在也查 'processing',先重置为 'received' 再 gateway 处理
    """
    return store.inbox_get_pending()


def reset_processing_orphan(store, event_id: str) -> bool:
    """
    把 'processing' 孤儿(前进程死前占用的)重置为 'received',
    让 gateway.handle_webhook 重新发起 inbox_check → 接管。
    返回 True=成功重置,False=不需要(已被 webhook 或别的 bridge 处理)。
    """
    return store.inbox_reset_processing(event_id)


def process_event(gw, store, event_id: str, raw_payload_json: str):
    """
    把 inbox event 转成 WebhookRequest,过 gateway.handle_webhook,返回 response。
    不直接发 Lark(发卡留给 webhook server 那侧,或在桥接器里用 LarkSender 发)。
    """
    from soundmirror.gateway.gateway import WebhookRequest
    from soundmirror.tools.lark_sender import HttpLarkSender
    from soundmirror.tools.contracts import load_lark_credentials_from_env

    if not raw_payload_json:
        log.warning(f"event {event_id} has no raw_payload, skip")
        return False

    try:
        payload = json.loads(raw_payload_json)
    except json.JSONDecodeError as e:
        log.error(f"event {event_id} invalid JSON: {e}")

    header = payload.get("header", {})
    event_type = header.get("event_type", "")

    # Parse based on event type
    if event_type == "im.message.receive_v1":
        msg = payload.get("event", {}).get("message", {})
        sender = payload.get("event", {}).get("sender", {})
        chat_id = msg.get("chat_id", "")
        message_id = msg.get("message_id", "")
        sender_open_id = sender.get("sender_id", {}).get("open_id", "")
        text = ""
        try:
            content_obj = json.loads(msg.get("content", "{}"))
            text = content_obj.get("text", "")
        except Exception:
            pass
        req = WebhookRequest(
            event_id=message_id,
            user_id=sender_open_id or f"anon_{chat_id}",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
        )
    elif event_type == "card.action.trigger_v1":
        event = payload.get("event", {})
        action = event.get("action", {})
        operator = event.get("operator", {})
        context = event.get("context", {})
        sender_open_id = operator.get("operator_id", {}).get("open_id", "")
        chat_id = context.get("open_chat_id", context.get("chat_id", ""))
        message_id = context.get("open_message_id", "")
        action_id = action.get("action_id", "")
        action_value = action.get("value", {})
        req = WebhookRequest(
            event_id=context.get("event_id", message_id),
            user_id=sender_open_id or f"anon_{chat_id}",
            chat_id=chat_id,
            message_id=message_id,
            text="",
            action_id=action_id,
            action_payload=action_value,
            raw=payload,
        )
    else:
        log.info(f"event {event_id} unknown type {event_type}, mark processed")
        return True

    log.info(f"processing event={req.event_id} action={req.action_id or '<text>'} text={req.text[:30]!r}")

    # 2026-09-22 改(Bridge E2E Failure 2):
    #   webhook 死时可能已经把 stage 推到下一阶段(如 proposal.confirm 把 stage
    #   推到 HIGHLIGHT_REVIEW),bridge 接管时 catalog 校验会拒绝(只允许 PROPOSAL_REVIEW)
    #   检测这种情况:session.stage 是 action 的允许阶段之后 + clips 全 PENDING
    #   → 把 stage 回退到 action 允许的阶段,让 handler 重新跑(检测 orphan retry)
    if req.action_id:
        try:
            cs_id = gw.session_resolver(req.user_id, req.chat_id)
            if cs_id:
                session = store.get_session(cs_id)
                stage = session["ledger"].get("current_stage", "INIT")
                clips = session["ledger"].get("highlight_clips", [])
                # 哪些 action 在 catalog 里允许哪些阶段?
                # 用本地简化:从 catalog 查
                from soundmirror.orchestrator.catalog import ActionCatalog
                cat = gw.orchestrator.catalog
                action_obj = cat.actions.get(req.action_id)
                if action_obj:
                    allowed = set(action_obj.allowed_stages or [])
                    all_pending = bool(clips) and all(c.get("status") == "PENDING" for c in clips)
                    if stage not in allowed and all_pending:
                        # stage 已推进但 generation 还没开始 → 回退 stage 让 action 重新触发
                        # 选最早的允许阶段
                        target_stage = sorted(allowed)[0] if allowed else stage
                        log.info(f"  rollback stage: {stage} → {target_stage} (clips all PENDING, "
                                 f"action {req.action_id} needs allowed_stages={list(allowed)})")
                        from soundmirror.storage.state_store import StateStoreError
                        store.apply_mutation(cs_id, {
                            "expected_revision": session["state_revision"],
                            "operations": [
                                {"op": "set", "path": "/current_stage", "value": target_stage},
                            ],
                        }, req.user_id)
        except Exception as e:
            log.warning(f"  bridge stage rollback check failed: {e}")

    # 处理 + 推 Lark
    try:
        resp = gw.handle_webhook(req)
        log.info(f"  gateway returned status={resp.status}")

        if resp.status == "duplicate":
            return True  # already done

        if resp.response_card:
            # 尝试用 LarkSender 发到 Lark
            creds = load_lark_credentials_from_env()
            if creds.app_id and creds.app_secret and "<REDACTED" not in creds.app_id:
                sender = HttpLarkSender(creds)
                try:
                    sent = sender.send_card(req.chat_id, resp.response_card)
                    log.info(f"  Lark send_card OK: msg_id={sent.message_id}")
                except Exception as e:
                    log.error(f"  Lark send_card failed: {e}")
            else:
                log.warning(f"  LARK credentials not set, response card not sent to Lark")

        # 标记 processed
        store.inbox_mark_processed(req.event_id)
        return True

    except Exception as e:
        log.error(f"  gateway exception: {e}", exc_info=True)
        return False


def scan_and_process(gw, store):
    """扫描 inbox 并处理所有 pending events"""
    pending = get_pending_events(store)
    if not pending:
        log.debug("no pending events")
        return 0

    log.info(f"found {len(pending)} pending events")
    success = 0
    for row in pending:
        # inbox_get_pending returns tuples (event_id, received_at, raw_payload)
        if isinstance(row, dict):
            event_id = row["event_id"]
            received_at = row["received_at"]
            raw_payload = row["raw_payload"]
        else:
            event_id, received_at, raw_payload = row
        log.info(f"processing {event_id} (received {received_at})")
        # 若 row 是 'processing' 孤儿(前 webhook 死)→ 重置为 'received'
        # 让 gateway.handle_webhook 的 inbox_check 走 UPDATE path 接管
        if store.inbox_reset_processing(event_id):
            log.info(f"  reset orphan {event_id} from 'processing' to 'received'")
        if process_event(gw, store, event_id, raw_payload):
            success += 1
    return success


def run_once():
    """跑一次 scan-and-process 后退出(供外部 cron 用)"""
    gw, store, _ = build_gateway()
    return scan_and_process(gw, store)


def run_loop(interval_seconds: int):
    """持续运行(等同 cron 守护进程)"""
    gw, store, db_path = build_gateway()
    log.info(f"bridge started, db={db_path}, interval={interval_seconds}s")

    running = True

    def handle_signal(signum, frame):
        nonlocal running
        log.info(f"got signal {signum}, shutting down")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    cycle = 0
    while running:
        cycle += 1
        try:
            n = scan_and_process(gw, store)
            if n:
                log.info(f"cycle {cycle}: processed {n}")
        except Exception as e:
            log.error(f"cycle {cycle}: error {e}", exc_info=True)

        # sleep in small chunks so signal works
        for _ in range(interval_seconds):
            if not running:
                break
            time.sleep(1)

    log.info("bridge stopped")


def main():
    parser = argparse.ArgumentParser(description="Cron Bridge Daemon")
    parser.add_argument("--once", action="store_true", help="scan once and exit (for cron)")
    parser.add_argument("--interval", type=int, default=30, help="poll interval seconds")
    args = parser.parse_args()

    if args.once:
        n = run_once()
        log.info(f"once-mode processed {n} events")
        sys.exit(0)
    else:
        run_loop(args.interval)


if __name__ == "__main__":
    main()
