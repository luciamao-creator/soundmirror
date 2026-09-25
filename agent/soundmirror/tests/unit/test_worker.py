"""
Worker Tests
"""
import os
import time
import pytest
import tempfile
import threading

from soundmirror.worker import (
    Worker, Job, JobQueue, WorkerPool, new_job,
    JobStatus, MusicWorker, RevisionWorker,
)
from soundmirror.tools import MockMusicPipeline, MockMusicAdapter


class TestJobLifecycle:
    def test_new_job_creation(self):
        job = new_job(
            job_type="music.generate",
            payload={"prompt": "A short folk ballad with warm guitar and gentle vocals"},
            actor_user_id="u_alice",
            creative_session_id="cs_xxx",
            state_revision=5,
        )
        assert job.job_id.startswith("job_")
        assert job.status == JobStatus.PENDING
        assert job.attempts == 0
        assert job.created_at > 0

    def test_job_state_transitions(self):
        job = new_job("music.generate", {}, "u_alice", "cs_xxx", 0)
        assert job.status == JobStatus.PENDING
        job.status = JobStatus.RUNNING
        assert job.status == JobStatus.RUNNING
        job.status = JobStatus.SUCCEEDED
        assert job.status == JobStatus.SUCCEEDED


class TestJobQueue:
    def test_submit_and_get(self):
        q = JobQueue()
        job = new_job("music.generate", {}, "u1", "cs1", 0)
        q.submit(job)
        assert q.get(job.job_id) is not None
        assert q.get(job.job_id).job_id == job.job_id

    def test_next_returns_submitted_job(self):
        q = JobQueue()
        job = new_job("music.generate", {}, "u1", "cs1", 0)
        q.submit(job)
        popped = q.next(timeout=0.5)
        assert popped is not None
        assert popped.job_id == job.job_id

    def test_list_jobs_by_status(self):
        q = JobQueue()
        j1 = new_job("music.generate", {}, "u1", "cs1", 0)
        j2 = new_job("music.generate", {}, "u1", "cs1", 0)
        j1.status = JobStatus.SUCCEEDED
        q.submit(j1)
        q.submit(j2)
        assert len(q.list_jobs(status=JobStatus.SUCCEEDED)) == 1
        assert len(q.list_jobs(status=JobStatus.PENDING)) == 1


class TestMusicWorker:
    def test_handle_job_with_mock_pipeline(self, tmp_path):
        pipeline = MockMusicPipeline()
        worker = MusicWorker(pipeline=pipeline, output_dir=str(tmp_path))

        job = new_job(
            "music.generate",
            {"prompt": "An acoustic folk ballad with warm vocals and gentle guitar",
             "lyrics": "[verse]\nHello world this is a test"},
            "u_alice",
            "cs_xxx",
            5,
        )
        result = worker.handle_job(job)

        assert result["node_id"].startswith("mock_node_")
        assert result["audio_url"]
        assert result["preview_path"] or result["error"]

    def test_handle_job_short_prompt_rejected(self):
        pipeline = MockMusicPipeline()
        worker = MusicWorker(pipeline=pipeline)

        job = new_job("music.generate", {"prompt": "short"}, "u1", "cs1", 0)
        with pytest.raises(ValueError):
            worker.handle_job(job)


class TestRevisionWorker:
    def test_revision_with_feedback_as_prompt(self, tmp_path):
        pipeline = MockMusicPipeline()
        worker = RevisionWorker(pipeline=pipeline, output_dir=str(tmp_path))

        job = new_job(
            "revision.generate",
            {"feedback": "副歌加 hook", "current_node_id": "old_node_123"},
            "u_alice",
            "cs_xxx",
            10,
        )
        result = worker.handle_job(job)
        assert result["node_id"]
        assert "副歌加 hook" in result["feedback_used"]


class TestWorkerPool:
    def test_pool_dispatches_to_correct_worker(self, tmp_path):
        pipeline = MockMusicPipeline()
        music_worker = MusicWorker(pipeline=pipeline, output_dir=str(tmp_path))

        q = JobQueue()
        pool = WorkerPool(q, [music_worker], num_threads=2)
        pool.start()
        try:
            job = new_job(
                "music.generate",
                {"prompt": "A short folk ballad with warm guitar and gentle vocals",
                 "lyrics": "[verse]\nTest"},
                "u_alice",
                "cs_xxx",
                5,
            )
            q.submit(job)

            # 等待 worker 完成
            for _ in range(20):
                if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                    break
                time.sleep(0.1)

            assert job.status == JobStatus.SUCCEEDED, f"job failed: {job.error}"
            assert job.result["node_id"]
            assert job.started_at is not None
            assert job.completed_at is not None
        finally:
            pool.stop()

    def test_pool_unknown_job_type_marks_failed(self, tmp_path):
        q = JobQueue()
        music_worker = MusicWorker(pipeline=MockMusicPipeline(), output_dir=str(tmp_path))
        pool = WorkerPool(q, [music_worker], num_threads=1)
        pool.start()
        try:
            job = new_job("unknown.type", {}, "u1", "cs1", 0)
            q.submit(job)

            for _ in range(20):
                if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
                    break
                time.sleep(0.1)

            assert job.status == JobStatus.FAILED
            assert "no worker" in (job.error or "")
        finally:
            pool.stop()
