"""
Highlight Flow Tests — PM Day 1 P0-1

验证 候选先生成、后选择 流程语义:
  - T1: clip.select 之前 generation 必须完成
  - T2: proposal.confirm 触发 3 个 candidate 生成
  - T3: 重复 proposal.confirm 幂等(不重新生成、不重上传)
  - T4: clip.select 只记录选择、不调用 pipeline
  - T5: 部分 slot FAILED 仍可继续
"""
import os
import pytest
import tempfile
import shutil

from soundmirror.storage import StateStore
from soundmirror.chat_repl import build_mutation_handlers


@pytest.fixture
def store(tmp_path):
    db = str(tmp_path / "highlight.db")
    return StateStore(db)


@pytest.fixture
def session(store):
    return store.create_session("u_test_day1")


@pytest.fixture
def handlers(store):
    return build_mutation_handlers(store)


def _seed_proposal_review(store, session):
    """把 session 推到 PROPOSAL_REVIEW(可直接 proposal.confirm 的状态)"""
    cs_id = session["creative_session_id"]
    rev = session["state_revision"]
    store.apply_mutation(cs_id, {
        "expected_revision": rev,
        "operations": [
            {"op": "set", "path": "/current_stage", "value": "PROPOSAL_REVIEW"},
            {"op": "set", "path": "/music_spec", "value": {
                "bpm": 88, "key": "C", "genre": "indie",
                "instruments": ["piano"],
            }},
            {"op": "set", "path": "/current_proposal", "value": {
                "title": "test",
                "lyrics": "[verse]\ntest\n[chorus]\ntest\n",
            }},
        ],
    }, "u_test_day1")


def _seed_highlight_review_no_clips(store, session):
    """session 在 HIGHLIGHT_REVIEW 但还没生成(用于 T1:拒绝测试)"""
    cs_id = session["creative_session_id"]
    rev = session["state_revision"]
    store.apply_mutation(cs_id, {
        "expected_revision": rev,
        "operations": [
            {"op": "set", "path": "/current_stage", "value": "HIGHLIGHT_REVIEW"},
            {"op": "set", "path": "/generation_status", "value": "GENERATING"},
        ],
    }, "u_test_day1")


def _seed_highlight_review_partial_failed(store, session):
    """seed 3 个 candidate,其中 A FAILED,B/C READY"""
    cs_id = session["creative_session_id"]
    rev = session["state_revision"]
    store.apply_mutation(cs_id, {
        "expected_revision": rev,
        "operations": [
            {"op": "set", "path": "/current_stage", "value": "HIGHLIGHT_REVIEW"},
            {"op": "set", "path": "/generation_status", "value": "READY_WITH_FAILURES"},
            {"op": "set", "path": "/highlight_clips", "value": [
                {"slot": "A", "status": "FAILED", "error": "ffmpeg timeout"},
                {"slot": "B", "status": "READY", "node_id": "n_B",
                 "preview_path": "/tmp/B.mp3", "url": "https://x/B"},
                {"slot": "C", "status": "READY", "node_id": "n_C",
                 "preview_path": "/tmp/C.mp3", "url": "https://x/C"},
            ]},
        ],
    }, "u_test_day1")


# ============================================================
# T1: clip.select 在 generation 完成前必须拒绝
# ============================================================

class TestHighlightFlow:
    def test_clip_select_before_generation_rejected(self, store, session, handlers):
        """未触发 proposal.confirm 时,clip.select 必须返回错误,不修改 ledger"""
        cs_id = session["creative_session_id"]
        _seed_highlight_review_no_clips(store, session)

        result = handlers["clip.select"](store, cs_id, {"slot": "A"}, "u_test_day1")

        # 必须返回错误
        assert isinstance(result, dict)
        assert result.get("error") == "GENERATION_NOT_READY"

        # ledger 不应被修改(没有 selected_highlight)
        sess2 = store.get_session(cs_id)
        assert "selected_highlight" not in sess2["ledger"]
        # stage 不变
        assert sess2["ledger"]["current_stage"] == "HIGHLIGHT_REVIEW"


# ============================================================
# T2: proposal.confirm 必须触发生成 3 个 candidate
# ============================================================

class TestProposalConfirmTriggersGeneration:
    def test_proposal_confirm_creates_job_and_clips(
        self, store, session, handlers, tmp_path
    ):
        """proposal.confirm 必须创建 generation_job_id + 3 个 clip 状态"""
        cs_id = session["creative_session_id"]
        _seed_proposal_review(store, session)

        out_dir = f"/workspace/voice-persona-assessment/agent/soundmirror/out/audio_highlight/{cs_id}"
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)

        handlers["proposal.confirm"](store, cs_id, {}, "u_test_day1")

        sess = store.get_session(cs_id)
        # 验证 stage
        assert sess["ledger"]["current_stage"] == "HIGHLIGHT_REVIEW"
        # 验证 job_id 存在
        assert sess["ledger"].get("generation_job_id", "").startswith("job_")
        # 验证 generation_status
        assert sess["ledger"].get("generation_status") in ("READY", "READY_WITH_FAILURES")
        # 验证 3 个 clip
        clips = sess["ledger"].get("highlight_clips", [])
        assert len(clips) == 3
        slots = sorted([c["slot"] for c in clips])
        assert slots == ["A", "B", "C"]
        # 验证每个 clip 有 job_id 关联
        for c in clips:
            assert c["generation_job_id"] == sess["ledger"]["generation_job_id"]
            # PM P1-2: 必须有来源标记
            assert c.get("is_mock") is True
            assert c.get("adapter") == "MockMusicPipeline"
            assert c.get("mime_type") == "audio/mpeg"

        # 清理
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)

    def test_proposal_confirm_silent_mp3_files_exist(
        self, store, session, handlers
    ):
        """3 个候选的 preview_path 必须真的存在且是合法 MP3"""
        cs_id = session["creative_session_id"]
        _seed_proposal_review(store, session)

        out_dir = f"/workspace/voice-persona-assessment/agent/soundmirror/out/audio_highlight/{cs_id}"
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)

        handlers["proposal.confirm"](store, cs_id, {}, "u_test_day1")

        sess = store.get_session(cs_id)
        clips = sess["ledger"].get("highlight_clips", [])

        ready_count = 0
        for c in clips:
            if c["status"] != "READY":
                continue
            ready_count += 1
            path = c["preview_path"]
            assert os.path.exists(path), f"file missing: {path}"
            # 验证文件大小 > 100 KB(30s 128k 应有 ~480KB)
            sz = os.path.getsize(path)
            assert sz > 100_000, f"file too small: {path} = {sz} bytes"
            # PM P1-2: 必须有 checksum
            assert c.get("checksum_sha256")
            assert len(c["checksum_sha256"]) == 64  # SHA-256 hex
            # PM P1-3: 必须有 expires_at
            assert c.get("signed_url_expires_at")

        # 至少 1 个应该成功
        assert ready_count >= 1

        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)


# ============================================================
# T3: 重复 proposal.confirm 幂等
# ============================================================

class TestProposalConfirmIdempotent:
    def test_repeat_proposal_confirm_does_not_regenerate(
        self, store, session, handlers
    ):
        """第二次 proposal.confirm 不重新生成,job_id 不变,文件 mtime 不变"""
        cs_id = session["creative_session_id"]
        _seed_proposal_review(store, session)

        out_dir = f"/workspace/voice-persona-assessment/agent/soundmirror/out/audio_highlight/{cs_id}"
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)

        # 第一次
        handlers["proposal.confirm"](store, cs_id, {}, "u_test_day1")
        sess1 = store.get_session(cs_id)
        job_id_1 = sess1["ledger"]["generation_job_id"]
        clips_1 = sess1["ledger"]["highlight_clips"]
        mtimes_1 = {}
        for c in clips_1:
            if c["status"] == "READY" and os.path.exists(c["preview_path"]):
                mtimes_1[c["slot"]] = os.path.getmtime(c["preview_path"])

        # 第二次
        handlers["proposal.confirm"](store, cs_id, {}, "u_test_day1")
        sess2 = store.get_session(cs_id)
        job_id_2 = sess2["ledger"]["generation_job_id"]
        clips_2 = sess2["ledger"]["highlight_clips"]

        # job_id 必须相同
        assert job_id_1 == job_id_2
        # node_id 必须相同(没重新生成)
        for c1, c2 in zip(clips_1, clips_2):
            if c1["status"] == "READY" and c2["status"] == "READY":
                assert c1["node_id"] == c2["node_id"], f"slot {c1['slot']} node_id changed"
                # mtime 不变
                if os.path.exists(c2["preview_path"]):
                    assert os.path.getmtime(c2["preview_path"]) == mtimes_1[c2["slot"]]

        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)


# ============================================================
# T4: clip.select 只记录选择,不调 pipeline
# ============================================================

class TestClipSelectRecordsOnly:
    def test_clip_select_does_not_call_pipeline(
        self, store, session, handlers, monkeypatch
    ):
        """clip.select 不应调用 MockMusicPipeline.generate_with_preview"""
        cs_id = session["creative_session_id"]
        _seed_proposal_review(store, session)
        handlers["proposal.confirm"](store, cs_id, {}, "u_test_day1")

        # spy on MockMusicPipeline
        from soundmirror.tools import MockMusicPipeline
        call_count = {"n": 0}
        original = MockMusicPipeline.generate_with_preview
        def spy(self, *args, **kwargs):
            call_count["n"] += 1
            return original(self, *args, **kwargs)
        monkeypatch.setattr(MockMusicPipeline, "generate_with_preview", spy)

        result = handlers["clip.select"](store, cs_id, {"slot": "A"}, "u_test_day1")

        # 不调用 pipeline
        assert call_count["n"] == 0, f"clip.select called pipeline {call_count['n']} times"
        # 返回 ok
        assert result.get("ok") is True
        assert result.get("selected_slot") == "A"
        # ledger 更新
        sess = store.get_session(cs_id)
        assert sess["ledger"]["selected_highlight"]["slot"] == "A"
        assert sess["ledger"]["selected_highlight"]["node_id"]
        assert sess["ledger"]["selected_highlight"]["selected_at"]
        assert sess["ledger"]["current_stage"] == "FULL_REVIEW"

        out_dir = f"/workspace/voice-persona-assessment/agent/soundmirror/out/audio_highlight/{cs_id}"
        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)


# ============================================================
# T5: 部分 slot FAILED 仍可继续选 READY
# ============================================================

class TestPartialFailureContinues:
    def test_select_ready_when_some_failed(
        self, store, session, handlers
    ):
        """A FAILED,B/C READY → 选 B 成功,选 A 拒绝"""
        cs_id = session["creative_session_id"]
        _seed_highlight_review_partial_failed(store, session)

        # 选 B(READY)→ 成功
        result_b = handlers["clip.select"](store, cs_id, {"slot": "B"}, "u_test_day1")
        assert result_b.get("ok") is True
        assert result_b.get("selected_slot") == "B"

        # 再造一个新 session 选 A(FAILED)→ 拒绝
        sess2_session = store.create_session("u_test_day1")
        cs_id2 = sess2_session["creative_session_id"]
        _seed_highlight_review_partial_failed(store, sess2_session)
        result_a = handlers["clip.select"](store, cs_id2, {"slot": "A"}, "u_test_day1")
        assert result_a.get("error") == "SLOT_NOT_READY"
        assert result_a.get("clip_status") == "FAILED"

    def test_select_unknown_slot_rejected(
        self, store, session, handlers
    ):
        """slot 不在 A/B/C → 拒绝"""
        cs_id = session["creative_session_id"]
        _seed_highlight_review_partial_failed(store, session)
        result = handlers["clip.select"](store, cs_id, {"slot": "Z"}, "u_test_day1")
        assert result.get("error") == "INVALID_SLOT"
