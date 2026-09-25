"""
Worker Architecture — 异步任务执行

Director 决策后,如果有 tool_request(比如 music.generate),就 push 到 Worker queue,
Worker 异步执行,不阻塞主对话流。

设计目标:
  1. 异步:tool_request 不阻塞 user-facing response
  2. 可观测:Worker 状态 + job 历史
  3. 可重启:Worker 进程死了重启,任务不丢
  4. 可替换:M1 用 in-memory queue,M2 换 Celery / RQ / Redis 队列

M1 实现:in-memory queue + threading.Thread worker pool
M2 升级方向:SQLite-backed outbox + asyncio / multiprocessing worker
"""
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Any


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    """Worker job descriptor"""
    job_id: str
    job_type: str                 # "music.generate" / "audio.clip" / "asset.upload"
    payload: dict
    actor_user_id: str
    creative_session_id: str
    state_revision: int
    status: JobStatus = JobStatus.PENDING
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    attempts: int = 0
    max_attempts: int = 3


class Worker:
    """Worker 基类 — 子类实现 handle_job"""

    job_type: str = ""

    def __init__(self, name: str = ""):
        self.name = name or self.job_type

    def handle_job(self, job: Job) -> dict:
        """处理一个 job,返回 result dict。子类必须实现。"""
        raise NotImplementedError


class JobQueue:
    """In-memory queue with audit trail

    不是持久的(M1 局限)。Worker 死了,jobs 丢。
    M2 应改成 SQLite-backed 或 Redis。
    """

    def __init__(self, max_size: int = 1000):
        self._q: queue.Queue = queue.Queue(maxsize=max_size)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, job: Job) -> str:
        with self._lock:
            self._jobs[job.job_id] = job
            self._q.put(job.job_id)
        return job.job_id

    def next(self, timeout: float = 1.0) -> Optional[Job]:
        try:
            job_id = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, status: Optional[JobStatus] = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if status:
            return [j for j in jobs if j.status == status]
        return jobs


class WorkerPool:
    """Thread pool that runs workers against a JobQueue"""

    def __init__(self, queue: JobQueue, workers: list[Worker], num_threads: int = 2):
        self.queue = queue
        self.workers = {w.job_type: w for w in workers}
        self.num_threads = num_threads
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self):
        for i in range(self.num_threads):
            t = threading.Thread(target=self._worker_loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()

    def _worker_loop(self):
        while not self._stop.is_set():
            job = self.queue.next(timeout=1.0)
            if job is None:
                continue
            self._process_one(job)

    def _process_one(self, job: Job):
        worker = self.workers.get(job.job_type)
        if not worker:
            self._mark_failed(job, f"no worker for job_type={job.job_type}")
            return

        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        job.attempts += 1
        try:
            result = worker.handle_job(job)
            job.result = result
            job.status = JobStatus.SUCCEEDED
            job.completed_at = time.time()
        except Exception as e:
            job.error = f"{type(e).__name__}: {e}"
            if job.attempts < job.max_attempts:
                job.status = JobStatus.PENDING
                # 重新入队
                self.queue.submit(job)
            else:
                self._mark_failed(job, job.error)
        finally:
            if job.completed_at is None and job.status == JobStatus.FAILED:
                job.completed_at = time.time()

    def _mark_failed(self, job: Job, error: str):
        job.status = JobStatus.FAILED
        job.error = error
        job.completed_at = time.time()


def new_job(job_type: str, payload: dict, actor_user_id: str,
            creative_session_id: str, state_revision: int) -> Job:
    return Job(
        job_id=f"job_{uuid.uuid4().hex[:12]}",
        job_type=job_type,
        payload=payload,
        actor_user_id=actor_user_id,
        creative_session_id=creative_session_id,
        state_revision=state_revision,
    )
