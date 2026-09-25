"""
State Store — SQLite-backed 乐观锁 + StateMutation 原子应用

核心合约(Golden Case 10 检验):
  - expected_revision == current_revision 才允许 apply
  - 否则抛 StaleRevisionError
  - 多个 operations 在同一事务里(全部成功 / 全部失败)
  - 单写队列:用 BEGIN IMMEDIATE 排队
  - revision 每次 +1,触发一次 event_published

Schema:
  - creative_sessions(id, user_id, revision, ledger, ...)
  - inbox(event_id, ...)  ← event 去重(Golden Case 11)
  - pending_actions(...) ← Orchestrator 用,这里只存
  - audit_log(...)        ← audit 记录
  - applied_mutations(...) ← Golden Case 11 幂等
"""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


class StateStoreError(Exception):
    pass


class SessionNotFoundError(StateStoreError):
    pass


class StaleRevisionError(StateStoreError):
    """Golden Case 10 触发"""
    def __init__(self, cs_id: str, expected: int, actual: int):
        self.cs_id = cs_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Stale revision on {cs_id}: expected={expected}, actual={actual}"
        )


class DuplicateEventError(StateStoreError):
    """Golden Case 11 触发"""
    def __init__(self, event_id: str):
        self.event_id = event_id
        super().__init__(f"Event {event_id} already processed")


class InboxResult:
    """inbox 去重的结果"""
    def __init__(self, is_duplicate: bool, first_seen_at: float):
        self.is_duplicate = is_duplicate
        self.first_seen_at = first_seen_at


class StateStore:
    """SQLite 单写队列 (BEGIN IMMEDIATE 序列化)

    简单但有效:M1 不需要真正的集群并发,用 SQLite + single writer 就够了。
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS creative_sessions (
        creative_session_id TEXT PRIMARY KEY,
        product_user_id TEXT NOT NULL,
        state_revision INTEGER NOT NULL DEFAULT 0,
        ledger TEXT NOT NULL DEFAULT '{}',
        started_at REAL NOT NULL,
        last_activity_at REAL NOT NULL,
        archived INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS inbox (
        event_id TEXT PRIMARY KEY,
        received_at REAL NOT NULL,
        raw_payload TEXT,
        status TEXT NOT NULL,
        attempt_count INTEGER DEFAULT 0,
        processed_at REAL
    );

    CREATE TABLE IF NOT EXISTS pending_actions (
        pending_action_id TEXT PRIMARY KEY,
        original_action_id TEXT NOT NULL,
        frozen_payload TEXT NOT NULL,
        actor_user_id TEXT NOT NULL,
        creative_session_id TEXT NOT NULL,
        state_revision INTEGER NOT NULL,
        expires_at REAL NOT NULL,
        status TEXT NOT NULL,
        created_at REAL NOT NULL,
        executed_at REAL
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        event_type TEXT NOT NULL,
        actor_user_id TEXT,
        creative_session_id TEXT,
        payload TEXT,
        outcome TEXT
    );
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self):
        # executescript 隐式 BEGIN/COMMIT 一次;隔离级别 EXCLUSIVE
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.executescript(self.SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def _conn(self):
        """autocommit 关闭,显式 BEGIN/COMMIT 走事务"""
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=10.0)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    # =========================================================
    #  Session Lifecycle
    # =========================================================

    def create_session(self, product_user_id: str, initial_ledger: Optional[dict] = None) -> dict:
        """新建 creative_session。返回完整 row + ledger"""
        cs_id = f"cs_{uuid.uuid4().hex[:12]}"
        ledger = json.dumps(initial_ledger or {}, ensure_ascii=False)
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                """
                INSERT INTO creative_sessions
                (creative_session_id, product_user_id, state_revision, ledger, started_at, last_activity_at)
                VALUES (?, ?, 0, ?, ?, ?)
                """,
                (cs_id, product_user_id, ledger, now, now),
            )
            c.execute("COMMIT")
            self._audit(c, "session.create", product_user_id, cs_id, {"initial_revision": 0})
        return {"creative_session_id": cs_id, "state_revision": 0, "ledger": initial_ledger or {}}

    def get_session(self, cs_id: str) -> Optional[dict]:
        """读完整 ledger(用于 LLM)。返回 {creative_session_id, state_revision, ledger, ...}"""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM creative_sessions WHERE creative_session_id = ?",
                (cs_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "creative_session_id": row["creative_session_id"],
                "product_user_id": row["product_user_id"],
                "state_revision": row["state_revision"],
                "ledger": json.loads(row["ledger"]),
                "started_at": row["started_at"],
                "last_activity_at": row["last_activity_at"],
                "archived": bool(row["archived"]),
            }

    def get_revision(self, cs_id: str) -> Optional[int]:
        with self._conn() as c:
            row = c.execute(
                "SELECT state_revision FROM creative_sessions WHERE creative_session_id = ?",
                (cs_id,),
            ).fetchone()
            return row["state_revision"] if row else None

    # =========================================================
    #  StateMutation — Golden Case 10 核心
    # =========================================================

    def apply_mutation(self, cs_id: str, mutation: dict, actor_user_id: str) -> dict:
        """应用 StateMutation 原子操作

        mutation 格式:
          {
            "expected_revision": 12,
            "operations": [
              {"op": "set", "path": "/music_spec/bpm", "value": 88},
              {"op": "incr", "path": "/generation_budget/used", "value": 1},
              {"op": "append", "path": "/history", "item": {...}},
              {"op": "del", "path": "/temp"}
            ]
          }

        重要: 审计日志用独立连接(autocommit),确保 ROLLBACK 不影响审计
        """
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute(
                    "SELECT state_revision, ledger, product_user_id FROM creative_sessions WHERE creative_session_id = ?",
                    (cs_id,),
                ).fetchone()
                if not row:
                    c.execute("ROLLBACK")
                    self._audit_standalone("session_not_found", actor_user_id, cs_id, {})
                    raise SessionNotFoundError(cs_id)

                # Golden Case 17: 跨用户写入必须拒绝
                if row["product_user_id"] != actor_user_id:
                    c.execute("ROLLBACK")
                    self._audit_standalone(
                        "cross_session_write_denied", actor_user_id, cs_id,
                        {"reason": "actor_mismatch", "actual_owner": row["product_user_id"]},
                    )
                    raise StateStoreError(
                        f"Permission denied: {actor_user_id} cannot write to {cs_id}"
                    )

                expected = mutation.get("expected_revision")
                if expected != row["state_revision"]:
                    c.execute("ROLLBACK")
                    self._audit_standalone(
                        "stale_revision_rejected", actor_user_id, cs_id,
                        {"expected": expected, "actual": row["state_revision"]},
                    )
                    raise StaleRevisionError(cs_id, expected, row["state_revision"])

                # 应用所有 operation
                ledger = json.loads(row["ledger"])
                for op in mutation.get("operations", []):
                    self._apply_op(ledger, op)

                new_rev = row["state_revision"] + 1
                new_ledger_str = json.dumps(ledger, ensure_ascii=False)
                now = time.time()
                c.execute(
                    "UPDATE creative_sessions SET state_revision = ?, ledger = ?, last_activity_at = ? "
                    "WHERE creative_session_id = ? AND state_revision = ?",
                    (new_rev, new_ledger_str, now, cs_id, row["state_revision"]),
                )
                c.execute("COMMIT")
                # 审计用独立连接,保证提交
                self._audit_standalone(
                    "state_mutation_applied", actor_user_id, cs_id,
                    {"old_revision": row["state_revision"], "new_revision": new_rev,
                     "ops": len(mutation.get("operations", []))},
                )
                return {"state_revision": new_rev, "ledger": ledger}
            except Exception as e:
                if not isinstance(e, StateStoreError):
                    c.execute("ROLLBACK")
                    raise StateStoreError(f"Unexpected: {e}") from e
                raise

    def _apply_op(self, ledger: dict, op: dict):
        """应用单条 operation,简化版 JSON Path (只支持 "/" 路径,a/b/c)"""
        path = op["path"]
        if not path.startswith("/"):
            raise ValueError(f"Path must start with /: {path}")

        segments = [s for s in path.split("/")[1:] if s]
        op_type = op["op"]

        if op_type == "set":
            self._set_path(ledger, segments, op["value"])
        elif op_type == "incr":
            current = self._get_path(ledger, segments) or 0
            self._set_path(ledger, segments, current + op["value"])
        elif op_type == "del":
            self._del_path(ledger, segments)
        elif op_type == "append":
            arr = self._get_path(ledger, segments)
            if arr is None:
                self._set_path(ledger, segments, [])
                arr = self._get_path(ledger, segments)
            arr.append(op["item"])
        elif op_type == "push":
            # 入栈,加 timestamp
            arr = self._get_path(ledger, segments)
            if arr is None:
                self._set_path(ledger, segments, [])
                arr = self._get_path(ledger, segments)
            arr.append(op["item"])
        else:
            raise ValueError(f"Unknown op: {op_type}")

    def _set_path(self, obj: dict, segments: list, value):
        for i, seg in enumerate(segments[:-1]):
            if seg not in obj or not isinstance(obj[seg], dict):
                obj[seg] = {}
            obj = obj[seg]
        obj[segments[-1]] = value

    def _get_path(self, obj: dict, segments: list):
        for seg in segments:
            if isinstance(obj, dict):
                obj = obj.get(seg)
            else:
                return None
            if obj is None:
                return None
        return obj

    def _del_path(self, obj: dict, segments: list):
        for i, seg in enumerate(segments[:-1]):
            if seg not in obj or not isinstance(obj[seg], dict):
                return
            obj = obj[seg]
        obj.pop(segments[-1], None)

    # =========================================================
    #  Inbox — Golden Case 11 幂等去重
    # =========================================================

    def inbox_check(self, event_id: str, raw_payload: Optional[str] = None) -> InboxResult:
        """原子地"领取"一个 event:status 转入 'processing'。
        - 如果是首次见到 → INSERT,status='processing'
        - 如果 row 已存在且 status='processed' → duplicate(别人已处理完)
        - 如果 row 已存在且 status='received' → 原子 UPDATE 为 'processing',接管
        - 如果 row 已存在且 status='processing' → duplicate(在跑中,别碰)

        2026-09-22 改(Failure 1 cron bridge):
          - row 已存在 status='received'/'processing' 不再算 duplicate
          - bridge 可接管 webhook 死前没处理完的 event
          - 但 'processing' 视为 in-flight,仍 duplicate(避免和 webhook 的 background thread 互打)
          - 用原子 UPDATE status=processing WHERE status=received 接管
        """
        now = time.time()
        with self._conn() as c:
            if raw_payload is not None:
                cur = c.execute(
                    "INSERT OR IGNORE INTO inbox (event_id, received_at, status, raw_payload) "
                    "VALUES (?, ?, 'processing', ?)",
                    (event_id, now, raw_payload),
                )
            else:
                cur = c.execute(
                    "INSERT OR IGNORE INTO inbox (event_id, received_at, status) "
                    "VALUES (?, ?, 'processing')",
                    (event_id, now),
                )
            if cur.rowcount == 1:
                # 我们是第一个:INSERT 成功,status='processing'
                return InboxResult(is_duplicate=False, first_seen_at=now)
            # row 已存在
            row = c.execute(
                "SELECT received_at, status, raw_payload FROM inbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row["status"] == "processed":
                # 真的处理过了
                return InboxResult(is_duplicate=True, first_seen_at=row["received_at"])
            # status = 'received' 或 'processing'
            if row["status"] == "received":
                # 接管:原子 UPDATE 为 processing(只在我们这一行时成功)
                upd = c.execute(
                    "UPDATE inbox SET status='processing', raw_payload=COALESCE(?, raw_payload) "
                    "WHERE event_id=? AND status='received'",
                    (raw_payload, event_id),
                )
                if upd.rowcount == 1:
                    return InboxResult(is_duplicate=False, first_seen_at=row["received_at"])
            # status = 'processing':别人正在跑 → duplicate
            return InboxResult(is_duplicate=True, first_seen_at=row["received_at"])

    def inbox_mark_processed(self, event_id: str):
        with self._conn() as c:
            c.execute(
                "UPDATE inbox SET status = 'processed', processed_at = ? WHERE event_id = ?",
                (time.time(), event_id),
            )

    def inbox_mark_processing(self, event_id: str):
        """bridge 取事件后,标记为 processing,避免并发重复 pick。
        普通 webhook 流程不调(直接 status=received→processed)。"""
        with self._conn() as c:
            c.execute(
                "UPDATE inbox SET status = 'processing' WHERE event_id = ? AND status = 'received'",
                (event_id,),
            )

    def inbox_persist(self, event_id: str, raw_payload: Optional[str] = None) -> bool:
        """2026-09-22 新增(Bridge E2E Failure 3 - data loss fix):
        同步持久化事件到 inbox,status='received'。

        与 inbox_check 不同:
          - inbox_check 会原子抢 row 到 'processing'(互斥),供 gateway 接管
          - inbox_persist 仅 INSERT 'received',**不抢**(留给 gateway.handle_webhook 接管)

        返回:
          True = 新插入(首次见到此 event_id)
          False = 已存在(duplicate,Lark 重发)

        用法:
          webhook server 在 ACK Lark 之前调用,保证 webhook 死时事件不丢。
          后续 gateway.handle_webhook 调 inbox_check 时仍会进入正常流程。
        """
        now = time.time()
        with self._conn() as c:
            if raw_payload is not None:
                cur = c.execute(
                    "INSERT OR IGNORE INTO inbox (event_id, received_at, status, raw_payload) "
                    "VALUES (?, ?, 'received', ?)",
                    (event_id, now, raw_payload),
                )
            else:
                cur = c.execute(
                    "INSERT OR IGNORE INTO inbox (event_id, received_at, status) "
                    "VALUES (?, ?, 'received')",
                    (event_id, now),
                )
            return cur.rowcount == 1

    def inbox_get_pending(self, limit: int = 50) -> list:
        """bridge 用:返回所有 status IN ('received', 'processing') 且 raw_payload 不为空的 row。

        2026-09-22 改(Failure 1 cron bridge):
          - webhook 死时,可能 status='processing'(已 inbox_check 但未完成)
          - bridge 应接管这些孤儿,重新发起处理
          - 'processed' 不取(已完成的)
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT event_id, received_at, raw_payload FROM inbox "
                "WHERE status IN ('received', 'processing') "
                "AND raw_payload IS NOT NULL "
                "ORDER BY received_at LIMIT ?",
                (limit,),
            ).fetchall()
            return [(r["event_id"], r["received_at"], r["raw_payload"]) for r in rows]

    def inbox_reset_processing(self, event_id: str) -> bool:
        """bridge 取到一个 status='processing' 的孤儿 → 重置为 'received',让 inbox_check 接管。

        必须是 SELECT-then-UPDATE 的原子对,避免 bridge 自己多个线程撞。
        返回 True=成功重置,False=没必要(已被别人处理)。
        """
        with self._conn() as c:
            cur = c.execute(
                "UPDATE inbox SET status='received' WHERE event_id=? AND status='processing'",
                (event_id,),
            )
            return cur.rowcount == 1

    def inbox_skip_duplicate(self, event_id: str) -> bool:
        """已存在则跳过。如果 event_id 不存在则返回 False。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM inbox WHERE event_id = ? AND status = 'processed'",
                (event_id,),
            ).fetchone()
            return row is not None

    # =========================================================
    #  PendingAction Store — Golden Case 09
    # =========================================================

    def create_pending_action(
        self,
        original_action_id: str,
        frozen_payload: dict,
        actor_user_id: str,
        creative_session_id: str,
        state_revision: int,
        ttl_seconds: int = 600,
    ) -> str:
        pa_id = f"pa_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO pending_actions
                (pending_action_id, original_action_id, frozen_payload, actor_user_id,
                 creative_session_id, state_revision, expires_at, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (pa_id, original_action_id, json.dumps(frozen_payload, ensure_ascii=False),
                 actor_user_id, creative_session_id, state_revision,
                 now + ttl_seconds, now),
            )
            self._audit(c, "pending_action_created", actor_user_id, creative_session_id,
                        {"pa_id": pa_id, "original_action": original_action_id})
        return pa_id

    def get_pending_action(self, pa_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM pending_actions WHERE pending_action_id = ?",
                (pa_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_pending(row)

    def _row_to_pending(self, row) -> dict:
        return {
            "pending_action_id": row["pending_action_id"],
            "original_action_id": row["original_action_id"],
            "frozen_payload": json.loads(row["frozen_payload"]),
            "actor_user_id": row["actor_user_id"],
            "creative_session_id": row["creative_session_id"],
            "state_revision": row["state_revision"],
            "expires_at": row["expires_at"],
            "status": row["status"],
            "created_at": row["created_at"],
            "executed_at": row["executed_at"],
        }

    def execute_pending(self, pa_id: str) -> dict:
        """Orchestrator 调用,把 status 从 pending → executed + 记录 executed_at。
        必须在确认业务执行成功后调用。
        """
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM pending_actions WHERE pending_action_id = ?",
                (pa_id,),
            ).fetchone()
            if not row:
                raise StateStoreError(f"No pending_action {pa_id}")
            if row["status"] != "pending":
                raise StateStoreError(f"Pending {pa_id} status is {row['status']} (expected pending)")
            if row["expires_at"] < time.time():
                c.execute(
                    "UPDATE pending_actions SET status = 'expired' WHERE pending_action_id = ? AND status = 'pending'",
                    (pa_id,),
                )
                c.execute("COMMIT")
                raise StateStoreError(f"Pending {pa_id} expired")
            now = time.time()
            c.execute(
                "UPDATE pending_actions SET status = 'executed', executed_at = ? WHERE pending_action_id = ? AND status = 'pending'",
                (now, pa_id),
            )
            self._audit(c, "pending_action_executed", row["actor_user_id"],
                        row["creative_session_id"], {"pa_id": pa_id})
            c.execute("COMMIT")
            return self._row_to_pending(row)

    def cancel_pending(self, pa_id: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM pending_actions WHERE pending_action_id = ? AND status = 'pending'",
                (pa_id,),
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE pending_actions SET status = 'cancelled' WHERE pending_action_id = ?",
                (pa_id,),
            )
            self._audit(c, "pending_action_cancelled", row["actor_user_id"],
                        row["creative_session_id"], {"pa_id": pa_id})
            return self._row_to_pending(row)

    # =========================================================
    #  Audit
    # =========================================================

    def _audit(self, conn, event_type: str, actor_user_id: Optional[str],
               cs_id: Optional[str], payload: dict, outcome: str = "ok"):
        conn.execute(
            "INSERT INTO audit_log (timestamp, event_type, actor_user_id, creative_session_id, payload, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), event_type, actor_user_id, cs_id,
             json.dumps(payload, ensure_ascii=False), outcome),
        )

    def _audit_standalone(self, event_type: str, actor_user_id: Optional[str],
                          cs_id: Optional[str], payload: dict, outcome: str = "ok"):
        """审计用独立连接,保证 ROLLBACK 不影响审计"""
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute(
                "INSERT INTO audit_log (timestamp, event_type, actor_user_id, creative_session_id, payload, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), event_type, actor_user_id, cs_id,
                 json.dumps(payload, ensure_ascii=False), outcome),
            )
        finally:
            conn.close()

    def read_audit(self, cs_id: str, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_log WHERE creative_session_id = ? ORDER BY id DESC LIMIT ?",
                (cs_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
