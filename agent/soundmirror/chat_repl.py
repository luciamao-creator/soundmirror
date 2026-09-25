"""
Chat REPL — 本地跟 SoundMirror Agent 对话

像用飞书一样:
  - 输入文本 → Agent 处理 → 渲染响应卡
  - 输入按钮编号 → 模拟点按钮 → 触发 action
  - 特殊命令:/state /ledger /audit /session /help /exit

直接 python -m soundmirror.chat_repl 启动。
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# 终端色码
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    GREY = "\033[90m"


# 卡片 header template → 终端色
TEMPLATE_COLOR = {
    "blue": C.BLUE,
    "green": C.GREEN,
    "red": C.RED,
    "yellow": C.YELLOW,
    "indigo": C.MAGENTA,
    "purple": C.MAGENTA,
    "grey": C.GREY,
    "orange": C.YELLOW,
}


# ============================================================
#  Mutation Handlers — 让 stage 推进按钮实际生效
# ============================================================


def _sha256_of_file(path: Optional[str]) -> Optional[str]:
    """文件 SHA-256 — 失败返 None(不抛)"""
    if not path or not os.path.exists(path):
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _utcnow_iso() -> str:
    """UTC ISO-8601 时间戳(秒级)"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _url_expires_in_1h() -> str:
    """1 小时后过期的 URL 过期时间(UTC ISO-8601)"""
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")


def build_mutation_handlers(store):
    """返回 orchestrator 的 mutation_handlers dict

    这些 handler 让按钮点下去真的推进 stage(不只是装饰)
    """
    handlers = {}

    def _archaeology_confirm(state_store, cs_id, payload, actor_user_id):
        """点击'够了' → 切到 IDENTITY_FITTING + 生成 3 个身份候选"""
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        # 用考古素材拼 3 个候选(简化 demo:M1 应由 director/LLM 出)
        facts = session["ledger"].get("archaeology_facts", [])
        snippet = facts[0]["text"][:12] if facts else "你的样子"
        candidates = [
            {"slot": "A", "label": f"贴耳 · {snippet}"},
            {"slot": "B", "label": f"自然 · {snippet}"},
            {"slot": "C", "label": f"辽阔 · {snippet}"},
        ]
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/current_stage", "value": "IDENTITY_FITTING"},
                {"op": "set", "path": "/identity_candidates", "value": candidates},
            ],
        }, actor_user_id)

    def _identity_select(state_store, cs_id, payload, actor_user_id):
        """identity.keep / identity.borrow / identity.reject → 写 persona_selections"""
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        action = payload.get("action", "keep")  # keep/borrow/reject
        slot = payload.get("slot", "?")
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "append", "path": "/persona_selections",
                 "item": {"action": action, "slot": slot}},
            ],
        }, actor_user_id)

        # 选完 → 进入 VARIABLE_SETUP
        sess2 = state_store.get_session(cs_id)
        state_store.apply_mutation(cs_id, {
            "expected_revision": sess2["state_revision"],
            "operations": [
                {"op": "set", "path": "/current_stage", "value": "VARIABLE_SETUP"},
                {"op": "set", "path": "/music_spec", "value": {
                    "bpm": 88,
                    "key": "C",
                    "genre": "indie",
                    "instruments": ["piano", "ambient pad"],
                }},
            ],
        }, actor_user_id)

    def _variable_set(state_store, cs_id, payload, actor_user_id):
        """variable.change → 写 music_spec 字段"""
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        spec = dict(session["ledger"].get("music_spec", {}))
        if "bpm" in payload:
            spec["bpm"] = payload["bpm"]
        if "key" in payload:
            spec["key"] = payload["key"]
        if "genre" in payload:
            spec["genre"] = payload["genre"]
        if "instrument" in payload:
            insts = list(spec.get("instruments", []))
            if payload["instrument"] not in insts:
                insts.append(payload["instrument"])
            spec["instruments"] = insts
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/music_spec", "value": spec},
            ],
        }, actor_user_id)

    def _skip_stage(state_store, cs_id, payload, actor_user_id):
        """stage.skip → 跳到下个 stage"""
        from .director import NEXT_STAGE
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        current = session["ledger"].get("current_stage")
        next_stage = NEXT_STAGE.get(current, "CONFIRMED")
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/current_stage", "value": next_stage},
            ],
        }, actor_user_id)

    def _stage_back(state_store, cs_id, payload, actor_user_id):
        """stage.back → 退到上 stage"""
        from .director import PREV_STAGE
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        current = session["ledger"].get("current_stage")
        prev = PREV_STAGE.get(current)
        if prev:
            state_store.apply_mutation(cs_id, {
                "expected_revision": rev,
                "operations": [
                    {"op": "set", "path": "/current_stage", "value": prev},
                ],
            }, actor_user_id)

    handlers["archaeology.confirm"] = _archaeology_confirm

    def _variable_confirm(state_store, cs_id, payload, actor_user_id):
        """variable.confirm → 进 PROPOSAL_REVIEW(显示歌词提案)

        2026-09-19 改(Day 4 验证):歌词从 archaeology_facts 拼出
          - V1.2:硬编码 stub
          - 现在:取用户最近 2 段考古素材拼成 verse/chorus
          - M2:接入 LLM 生成
        """
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        music_spec = session["ledger"].get("music_spec", {})
        facts = session["ledger"].get("archaeology_facts", [])

        # 从考古素材拼歌词
        verse_lines = []
        for f in facts[:2]:
            text = f.get("text", "") if isinstance(f, dict) else str(f)
            # 取第一句
            first = text.split("。")[0].split(",")[0].strip()
            if first and len(first) <= 30:
                verse_lines.append(first)
        if not verse_lines:
            verse_lines = ["海风停了 傍晚的光", "你不在 但影子还在"]

        lyrics = "[verse]\n" + "\n".join(verse_lines) + "\n\n[chorus]\n你不在 但影子还在\n"

        proposal = {
            "title": "未命名",
            "lyrics": lyrics,
            "music_spec_snapshot": dict(music_spec),
            "persona_selections": list(session["ledger"].get("persona_selections", [])),
            "source_facts_count": len(facts),
        }
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/current_stage", "value": "PROPOSAL_REVIEW"},
                {"op": "set", "path": "/current_proposal", "value": proposal},
            ],
        }, actor_user_id)

    def _proposal_confirm(state_store, cs_id, payload, actor_user_id):
        """proposal.confirm → 触发生成 3 个候选 → 进 HIGHLIGHT_REVIEW

        2026-09-19 改(PM Day 1 P0-1):
          - 之前:btn:1(✓ 接受方案)只切 stage,真正生成在 btn:1(🎧 选 A)时发生 — 语义反
          - 现在:proposal.confirm 同步触发生成 3 个 candidate(状态:READY/FAILED)
          - 高亮选择时 clip.select 只记录选择,不再次生成
          - 幂等:同一 session 重复 confirm 不重生成、不重上传
          - 强制来源标记:is_mock=True / adapter=MockMusicPipeline / checksum_sha256

        2026-09-19 改(Day 4 验证):支持真 Matrix 生成
          - 环境变量 SOUNDMIRROR_USE_REAL_MATRIX=1 → 用 MusicPipeline(prefer_real=True)
          - 否则继续用 MockMusicPipeline(默认)
          - 3 个候选并行(顺序,但用同一 pipeline 实例)
        """
        use_real_matrix = os.environ.get("SOUNDMIRROR_USE_REAL_MATRIX", "0") == "1"

        if use_real_matrix:
            from .tools import MusicPipeline
            from .tools.music_pipeline import FALLBACK_POLICY_REAL_REQUIRED
            from .tools.matrix_adapter import MatrixMusicAdapter
            # Matrix 实际 77-90s,variance 大,给 180s 余量
            real_adapter = MatrixMusicAdapter(timeout_seconds=180)
            pipeline = MusicPipeline(
                music_gen=real_adapter,
                prefer_real=True,
                fallback_policy=FALLBACK_POLICY_REAL_REQUIRED,
            )
            adapter_name = "MusicPipeline(real_matrix, real_required, timeout=180s)"
        else:
            from .tools import MockMusicPipeline
            pipeline = MockMusicPipeline()
            adapter_name = "MockMusicPipeline"

        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        existing_job = session["ledger"].get("generation_job_id")

        # 幂等:已有 job 则不重启
        if existing_job and session["ledger"].get("generation_status") in ("READY", "GENERATING"):
            # 仅刷新 stage(允许回到 HIGHLIGHT_REVIEW)
            state_store.apply_mutation(cs_id, {
                "expected_revision": rev,
                "operations": [
                    {"op": "set", "path": "/current_stage", "value": "HIGHLIGHT_REVIEW"},
                ],
            }, actor_user_id)
            return

        # 1) 创建 job(PENDING clips)
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        pending_clips = [
            {"slot": s, "status": "PENDING", "generation_job_id": job_id}
            for s in ("A", "B", "C")
        ]
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/generation_job_id", "value": job_id},
                {"op": "set", "path": "/generation_status", "value": "GENERATING"},
                {"op": "set", "/highlight_clips": pending_clips} if False else {"op": "set", "path": "/highlight_clips", "value": pending_clips},
                {"op": "set", "path": "/current_stage", "value": "HIGHLIGHT_REVIEW"},
            ],
        }, actor_user_id)

        # 2) 同步生成 3 个候选(M2 改异步 worker queue)
        music_spec = session["ledger"].get("music_spec", {})
        proposal = session["ledger"].get("current_proposal", {})
        lyrics = (proposal.get("lyrics", "") if proposal else "")
        if not lyrics:
            lyrics = "[verse]\n海风停了,傍晚的光\n[chorus]\n你不在 但影子还在\n"

        out_dir = f"/workspace/voice-persona-assessment/agent/soundmirror/out/audio_highlight/{cs_id}"
        os.makedirs(out_dir, exist_ok=True)

        # 2026-09-19 改(语言对齐 bug 修复):
        # 之前:prompt 一直用英文,歌词是中文时 Matrix 默认英文唱腔 → 半中半英
        # 现在:歌词含中文 → 用中文 prompt;否则用英文
        has_chinese = any('\u4e00' <= c <= '\u9fff' for c in lyrics)
        if has_chinese:
            # 中文 prompt 模板:自然描述,带 BPM/key/genre/instruments
            bpm = music_spec.get('bpm', '?')
            key = music_spec.get('key', '?')
            genre = music_spec.get('genre', 'indie')
            instruments = music_spec.get('instruments', [])
            inst_str = "、".join(instruments) if instruments else "吉他"
            slot_label = {"A": "贴耳", "B": "自然", "C": "辽阔"}
            prompt_str = (
                f"一首中文{genre}歌曲,BPM {bpm},调式 {key},配器包括 {inst_str},"
                f"中文女声演唱,verse_"
            )
            slot_suffix = slot_label  # used later per slot
        else:
            prompt_str = (
                f"bpm={music_spec.get('bpm','?')}, "
                f"key={music_spec.get('key','?')}, "
                f"genre={music_spec.get('genre','?')}, "
                f"instruments={music_spec.get('instruments', [])}, variant_"
            )
            slot_suffix = {"A": "A", "B": "B", "C": "C"}
        print(f"  [Mavis] Using {adapter_name}", flush=True)
        print(f"  [Mavis] lyrics_lang={'zh' if has_chinese else 'en'}", flush=True)
        print(f"  [Mavis] Generating 3 candidates for cs={cs_id}...", flush=True)

        ready_clips = []
        any_failed = False
        for s in ("A", "B", "C"):
            try:
                slot_prompt = prompt_str + slot_suffix[s] + "风格"
                result = pipeline.generate_with_preview(
                    prompt=slot_prompt,
                    lyrics=lyrics,
                    output_dir=out_dir,
                    preview_seconds=30,
                )
                ok = result.get("uploaded", False) and result.get("preview_path")
                preview_path = result.get("preview_path")
                checksum = _sha256_of_file(preview_path) if ok else None
                # 检查实际 adapter:real Matrix 返回的 node_id 是数字串,mock 是 mock_node_NNNNN
                music = result.get("music", None)
                node_id = music.node_id if music else None
                is_real = (node_id and not node_id.startswith("mock_node_")
                           and not node_id.startswith("mock_full_"))
                ready_clips.append({
                    "slot": s,
                    "status": "READY" if ok else "FAILED",
                    "node_id": node_id,
                    "preview_path": preview_path,
                    "url": result.get("preview_url"),
                    "duration_ms": 30000,
                    "is_mock": not is_real,
                    "adapter": adapter_name,
                    "checksum_sha256": checksum,
                    "mime_type": "audio/mpeg",
                    "signed_url_expires_at": _url_expires_in_1h(),
                    "generation_job_id": job_id,
                    "error": result.get("error"),
                })
                print(f"  [Mavis] slot {s}: {'REAL' if is_real else 'MOCK'} node_id={node_id} "
                      f"duration={os.path.getsize(preview_path) if preview_path and os.path.exists(preview_path) else 0} bytes",
                      flush=True)
                if not ok:
                    any_failed = True
            except Exception as e:
                any_failed = True
                ready_clips.append({
                    "slot": s,
                    "status": "FAILED",
                    "is_mock": True,
                    "adapter": "MockMusicPipeline",
                    "generation_job_id": job_id,
                    "error": str(e),
                })

        # 3) 更新 clip 状态 + job 状态
        sess2 = state_store.get_session(cs_id)
        new_rev = sess2["state_revision"]
        # job_status 映射:
        #  - 全部 READY → READY
        #  - 全部 FAILED → FAILED(用户无法选)
        #  - 部分成功 → READY_WITH_FAILURES(降级可用)
        success_count = sum(1 for c in ready_clips if c["status"] == "READY")
        fail_count = sum(1 for c in ready_clips if c["status"] == "FAILED")
        if fail_count == 0:
            job_status = "READY"
        elif success_count == 0:
            job_status = "FAILED"
        else:
            job_status = "READY_WITH_FAILURES"
        state_store.apply_mutation(cs_id, {
            "expected_revision": new_rev,
            "operations": [
                {"op": "set", "path": "/highlight_clips", "value": ready_clips},
                {"op": "set", "path": "/generation_status", "value": job_status},
            ],
        }, actor_user_id)

    def _review_approve(state_store, cs_id, payload, actor_user_id):
        """review.approve → 进 CONFIRMED"""
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/current_stage", "value": "CONFIRMED"},
            ],
        }, actor_user_id)

    def _highlight_select(state_store, cs_id, payload, actor_user_id):
        """clip.select / highlight.select → 校验 + 选 + 自动生成完整歌曲

        2026-09-19 改(PM Day 1 P0-1):
          - 之前:clip.select 在这里生成 3 个 candidate + 记录选择 — 语义反
          - 现在:候选已在 _proposal_confirm 阶段生成完毕,这里只校验 + 记录
          - 拒绝条件:
            1. generation_status 不是 READY/READY_WITH_FAILURES
            2. 目标 slot 状态非 READY
          - 拒绝时不修改 ledger,允许前端再问
          - 选择后:
            1. selected_highlight 记录 slot + node_id + 时间戳
            2. 触发 FULL_REVIEW stage

        2026-09-20 改(补全能力):
          - 之前:完整歌曲不被生成,只保留 30s 试听
          - 现在:用选中 highlight 的 node_id 调 MusicPipeline.generate_full_song_from_node
                下载完整歌曲(不剪)→ 上传 OSS → 存 full_song_path/url/duration_ms/sha256
                ledger.selected_highlight 完整记录
        """
        slot = payload.get("slot", "A")
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]

        # 1) 校验 generation 已完成
        gen_status = session["ledger"].get("generation_status")
        if gen_status not in ("READY", "READY_WITH_FAILURES"):
            return {
                "error": "GENERATION_NOT_READY",
                "message": f"候选尚未生成完成(current={gen_status})",
                "current_stage": session["ledger"].get("current_stage"),
            }

        # 2) 校验目标 slot
        clips = session["ledger"].get("highlight_clips", [])
        target = next((c for c in clips if c.get("slot") == slot), None)
        if not target:
            return {"error": "INVALID_SLOT", "slot": slot}
        if target.get("status") != "READY":
            return {
                "error": "SLOT_NOT_READY",
                "slot": slot,
                "clip_status": target.get("status"),
                "message": f"Slot {slot} 状态 {target.get('status')},无法选择",
            }

        # 3) 生成完整歌曲(从选中 highlight 的 node_id)
        target_node_id = target.get("node_id")
        full_song_dir = (
            f"/workspace/voice-persona-assessment/agent/soundmirror/out/audio_full/{cs_id}"
        )
        os.makedirs(full_song_dir, exist_ok=True)

        # 决定用哪个 pipeline(和 proposal.confirm 同源,保持一致)
        use_real_matrix = os.environ.get("SOUNDMIRROR_USE_REAL_MATRIX", "0") == "1"
        if use_real_matrix:
            from .tools import MusicPipeline as _MP
            from .tools.music_pipeline import FALLBACK_POLICY_REAL_REQUIRED
            from .tools.matrix_adapter import MatrixMusicAdapter
            full_pipeline = _MP(
                music_gen=MatrixMusicAdapter(timeout_seconds=60),
                prefer_real=True,
                fallback_policy=FALLBACK_POLICY_REAL_REQUIRED,
            )
        else:
            from .tools import MockMusicPipeline
            # Mock 路径下没有真实 node_id 资产,无法 get_audio_url
            # 用本地 full_song_path = None 表示无法生成
            full_pipeline = MockMusicPipeline()

        full_song = full_pipeline.generate_full_song_from_node(target_node_id, full_song_dir)
        print(f"  [Mavis] full song for slot {slot}: {full_song.get('duration_ms', '?')}ms, "
              f"uploaded={full_song.get('uploaded', False)}, err={full_song.get('error') or '-'}",
              flush=True)

        # 4) 单次 mutation:记录选择 + 完整歌曲 + 推进 stage
        selected_value = {
            "slot": slot,
            "node_id": target.get("node_id"),
            "preview_path": target.get("preview_path"),
            "preview_url": target.get("url"),
            "preview_duration_ms": target.get("duration_ms", 30000),
            "selected_at": _utcnow_iso(),
            # 2026-09-20:完整歌曲
            "full_song_path": full_song.get("full_song_path"),
            "full_song_url": full_song.get("full_song_url"),
            "full_song_duration_ms": full_song.get("duration_ms"),
            "full_song_bytes": full_song.get("bytes"),
            "full_song_checksum_sha256": full_song.get("checksum_sha256"),
            "full_song_signed_url_expires_at": full_song.get("signed_url_expires_at"),
            "full_song_adapter": full_song.get("adapter"),
            "full_song_error": full_song.get("error"),
        }
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/current_stage", "value": "FULL_REVIEW"},
                {"op": "set", "path": "/selected_highlight", "value": selected_value},
            ],
        }, actor_user_id)
        return {"ok": True, "selected_slot": slot, "stage": "FULL_REVIEW",
                "full_song_generated": bool(full_song.get("full_song_path")),
                "full_song_duration_ms": full_song.get("duration_ms")}

    def _full_confirm(state_store, cs_id, payload, actor_user_id):
        """full.confirm → 进 CONFIRMED"""
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/current_stage", "value": "CONFIRMED"},
            ],
        }, actor_user_id)

    def _profile_archive(state_store, cs_id, payload, actor_user_id):
        """profile.archive → 进 PROFILE_ARCHIVED"""
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/current_stage", "value": "PROFILE_ARCHIVED"},
            ],
        }, actor_user_id)

    handlers["identity.keep"] = lambda *a, **kw: _identity_select(*a, action="keep", **kw) if False else None
    # 直接绑一个 closure
    def _identity_keep(s, c, p, u):
        _identity_select(s, c, {**p, "action": "keep"}, u)
    def _identity_borrow(s, c, p, u):
        _identity_select(s, c, {**p, "action": "borrow"}, u)
    def _identity_reject(s, c, p, u):
        _identity_select(s, c, {**p, "action": "reject"}, u)

    handlers["identity.keep"] = _identity_keep
    handlers["identity.borrow"] = _identity_borrow
    handlers["identity.reject"] = _identity_reject
    handlers["variable.change"] = _variable_set
    handlers["variable.confirm"] = _variable_confirm
    handlers["proposal.confirm"] = _proposal_confirm
    handlers["review.approve"] = _review_approve
    handlers["highlight.select"] = _highlight_select
    handlers["clip.select"] = _highlight_select
    handlers["full.confirm"] = _full_confirm
    handlers["profile.archive"] = _profile_archive
    handlers["stage.skip"] = _skip_stage
    handlers["stage.back"] = _stage_back

    return handlers


class ChatRepl:
    def __init__(self, user_id: str = "u_demo", chat_id: str = "oc_demo",
                 db_path: Optional[str] = None, fixtures: Optional[str] = None):
        # 装配 Gateway
        from soundmirror.storage import StateStore
        from soundmirror.orchestrator import ActionCatalog
        from soundmirror.orchestrator.orchestrator import Orchestrator
        from soundmirror.director import Director
        from soundmirror.stage import StageProcessor
        from soundmirror.gateway import LarkCardV1Renderer
        from soundmirror.gateway.gateway import Gateway, WebhookRequest

        if not db_path:
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            db_path = tmp.name

        store = StateStore(db_path)
        catalog = ActionCatalog(str(Path(__file__).parent / "action_catalog.json"))
        orchestrator = Orchestrator(catalog, store, mutation_handlers=build_mutation_handlers(store))
        director = Director(orchestrator=orchestrator, store=store)
        stage_proc = StageProcessor(orchestrator, store)
        renderer = LarkCardV1Renderer()

        self.store = store
        self.director = director
        self.gateway = Gateway(
            state_store=store,
            orchestrator=orchestrator,
            director=director,
            stage_processor=stage_proc,
            card_renderer=renderer,
        )
        self.user_id = user_id
        self.chat_id = chat_id
        self.last_pending_id = None  # 上次响应卡里的 pending_action_id
        self.session_id = None        # 用户的 creative_session_id

        if fixtures:
            self._auto_replay_first_message(fixtures)

    def _auto_replay_first_message(self, text: str):
        """如果给了 --auto-fixtures,自动发第一条消息(通常用 'hi')"""
        self.process_input(text)

    def process_input(self, user_text: str) -> None:
        """处理一条用户输入(文本或按钮)"""
        from soundmirror.gateway.gateway import WebhookRequest

        event_id = f"evt_repl_{uuid.uuid4().hex[:8]}"

        if user_text.startswith("btn:"):
            # 按钮语法:btn:<index>
            try:
                idx = int(user_text.split(":", 1)[1].strip()) - 1
            except (ValueError, IndexError):
                print(f"{C.RED}✗ 用法:btn:<编号>{C.RESET}")
                return

            if not hasattr(self, "_last_card_actions") or idx >= len(self._last_card_actions):
                print(f"{C.RED}✗ 没那个按钮{'(1-%d)' % len(getattr(self, '_last_card_actions', []))}{C.RESET}")
                return

            action = self._last_card_actions[idx]
            # Lark button 格式:text/type/value{action_id, ...}
            # 兼容老格式:top-level action_id
            action_value = action.get("value", {})
            if "action_id" in action_value:
                action_id = action_value["action_id"]
            else:
                action_id = action.get("action_id", "")

            # 自动带上上次的 pending_action_id
            if action_id in ("confirm.accept", "confirm.cancel") and "pending_action_id" not in action_value:
                if self.last_pending_id:
                    action_value["pending_action_id"] = self.last_pending_id

            req = WebhookRequest(
                event_id=event_id,
                user_id=self.user_id,
                chat_id=self.chat_id,
                message_id=f"om_repl_{event_id}",
                text="",
                action_id=action_id,
                action_payload=action_value,
            )
        elif user_text.startswith("/admin"):
            # admin 推 stage 用,只本地用
            parts = user_text.split(" ", 1)
            if len(parts) < 2:
                print(f"{C.RED}✗ 用法:/admin <json>{C.RESET}")
                return
            try:
                mutation = json.loads(parts[1])
                self._admin_state_mutation(mutation)
            except json.JSONDecodeError as e:
                print(f"{C.RED}✗ JSON 解析失败:{e}{C.RESET}")
            return
        else:
            req = WebhookRequest(
                event_id=event_id,
                user_id=self.user_id,
                chat_id=self.chat_id,
                message_id=f"om_repl_{event_id}",
                text=user_text,
            )

        resp = self.gateway.handle_webhook(req)
        self._render_response(resp)

    def _admin_state_mutation(self, mutation: dict):
        """本地 admin:直接 push state_mutation,跳过 Gateway"""
        sessions = self.gateway._list_user_sessions(self.user_id)
        if not sessions:
            print(f"{C.RED}✗ 没有 active session,先发 hi{C.RESET}")
            return
        cs_id = sessions[0]["creative_session_id"]
        try:
            self.gateway.store.apply_mutation(cs_id, mutation, self.user_id)
            print(f"{C.GREEN}✓ admin mutation applied:{C.DIM} {mutation.get('operations')}{C.RESET}")
        except Exception as e:
            print(f"{C.RED}✗ mutation failed:{e}{C.RESET}")

    def _render_response(self, resp):
        """把 GatewayResponse 渲染成好看的样子"""
        if resp.status == "duplicate":
            print(f"{C.GREY}  (event_id 已处理,跳过){C.RESET}\n")
            return

        if resp.status == "error":
            print(f"{C.RED}  ✗ error:{resp.error}{C.RESET}\n")
            return

        # text reply
        if resp.text_reply:
            print(f"  {C.GREEN}{resp.text_reply}{C.RESET}\n")
            return

        # card
        if not resp.response_card:
            print(f"  {C.GREY}(no card){C.RESET}\n")
            return

        self._render_card(resp.response_card)

    def _render_card(self, card: dict):
        header = card.get("header", {})
        title = header.get("title", {}).get("content", "?")
        template = header.get("template", "blue")
        color = TEMPLATE_COLOR.get(template, C.RESET)

        # 框
        bar = "─" * 60
        print(f"\n{C.DIM}┌{bar}┐{C.RESET}")
        print(f"{C.DIM}│{C.RESET} {color}{C.BOLD}▌ {title}{C.RESET}")
        print(f"{C.DIM}│{C.RESET} {C.DIM}(template: {template}){C.RESET}")

        # 收集 actions
        self._last_card_actions = []

        for elem in card.get("elements", []):
            tag = elem.get("tag")
            if tag == "div":
                text = elem.get("text", {}).get("content", "")
                print(f"{C.DIM}│{C.RESET}   {text}")
            elif tag == "note":
                content = "".join(
                    e.get("content", "") for e in elem.get("elements", [])
                )
                print(f"{C.DIM}│{C.RESET}   {C.GREY}📌 {content}{C.RESET}")
            elif tag == "hr":
                print(f"{C.DIM}│{C.RESET} {C.DIM}   ───────────────────────────{C.RESET}")
            elif tag == "action":
                buttons = elem.get("actions", [])
                for j, btn in enumerate(buttons):
                    label = btn.get("text", {}).get("content", "?")
                    btn_type = btn.get("type", "?")
                    danger = " ⚠️" if btn_type == "danger" else ""
                    idx = len(self._last_card_actions) + 1
                    self._last_card_actions.append(btn)
                    print(f"{C.DIM}│{C.RESET}   {color}[btn:{idx}]{C.RESET} {label}{danger}")

        print(f"{C.DIM}└{bar}┘{C.RESET}")

        # 自动记下 pending_action_id
        for elem in card.get("elements", []):
            if elem.get("tag") == "action":
                for btn in elem.get("actions", []):
                    pid = btn.get("value", {}).get("pending_action_id")
                    if pid:
                        self.last_pending_id = pid

        print()

    def print_state(self):
        """显示当前 session 状态"""
        sessions = self.gateway._list_user_sessions(self.user_id)
        if not sessions:
            print(f"{C.GREY}(no session yet){C.RESET}")
            return
        cs_id = sessions[0]["creative_session_id"]
        self.session_id = cs_id
        session = self.store.get_session(cs_id)
        ledger = session["ledger"]
        rev = session["state_revision"]
        stage = ledger.get("current_stage", "?")
        print(f"\n{C.CYAN}{C.BOLD}Session State{C.RESET}")
        print(f"  cs_id:    {C.DIM}{cs_id}{C.RESET}")
        print(f"  revision: {C.YELLOW}{rev}{C.RESET}")
        print(f"  stage:    {C.GREEN}{stage}{C.RESET}")
        # 摘要
        facts = ledger.get("archaeology_facts", [])
        if facts:
            print(f"  archaeology_facts: {len(facts)} 条")
            for f in facts[-2:]:
                print(f"    - {f['text'][:50]}")
        music_spec = ledger.get("music_spec", {})
        if music_spec:
            print(f"  music_spec: {music_spec}")
        selections = ledger.get("persona_selections", [])
        if selections:
            print(f"  persona_selections: {selections}")
        edits = ledger.get("proposal_edits", [])
        if edits:
            print(f"  proposal_edits: {len(edits)} 条")
        print()

    def print_audit(self, limit: int = 10):
        sessions = self.gateway._list_user_sessions(self.user_id)
        if not sessions:
            print(f"{C.GREY}(no session){C.RESET}")
            return
        cs_id = sessions[0]["creative_session_id"]
        events = self.store.read_audit(cs_id, limit=limit)
        print(f"\n{C.CYAN}{C.BOLD}Audit Log (last {limit}){C.RESET}")
        for e in reversed(events):
            et = e["event_type"]
            ts = e.get("timestamp", 0)
            payload = e.get("payload", "")
            print(f"  {C.DIM}{ts:.0f}{C.RESET} {C.YELLOW}{et}{C.RESET} {C.GREY}{payload[:80]}{C.RESET}")
        print()

    def print_help(self):
        print(f"""
{C.CYAN}{C.BOLD}Commands{C.RESET}
  直接输入文字            → Agent 解析 → 输出卡
  btn:<N>                 → 模拟按第 N 个按钮(从卡的顺序数)
  /state                  → 看当前 session 状态 (stage / revision / ledger)
  /ledger                 → 看完整 ledger JSON
  /audit [N]              → 看 audit log(默认 10 条)
  /session                → 重开一个新 session
  /admin <json>           → 直接 push state_mutation(本地调试用)
  /help                   → 这个帮助
  /exit                   → 退出

{C.CYAN}{C.BOLD}Tips{C.RESET}
  - 第一次输入 'hi' 创建 session
  - 'BPM 88' / '降 B 大调' / '换成电子' / '加钢琴' → 改变量
  - '保留 a' / '保留 b' / '保留 c' → 选身份
  - '跳过' / '上一步' → 阶段控制
  - '整体满意' / '副歌加 hook' → 评审
{C.RESET}
""")

    def print_full_ledger(self):
        sessions = self.gateway._list_user_sessions(self.user_id)
        if not sessions:
            print(f"{C.GREY}(no session){C.RESET}")
            return
        cs_id = sessions[0]["creative_session_id"]
        session = self.store.get_session(cs_id)
        print(f"\n{C.CYAN}Full Ledger:{C.RESET}")
        print(json.dumps(session["ledger"], ensure_ascii=False, indent=2))
        print()

    def run(self):
        print(f"\n{C.CYAN}{C.BOLD}╔══════════════════════════════════════╗")
        print(f"║  SoundMirror Chat REPL             ║")
        print(f"║  user_id = {self.user_id:20s} ║")
        print(f"║  chat_id = {self.chat_id:20s} ║")
        print(f"║  输入 /help 看命令                  ║")
        print(f"╚══════════════════════════════════════╝{C.RESET}\n")

        if self.session_id is None:
            # 自动发 hi 创建 session
            pass

        print(f"{C.GREY}提示:输入 'hi' 开始 / /help 看命令 / /exit 退出{C.RESET}\n")

        while True:
            try:
                line = input(f"{C.BOLD}{C.CYAN}you>{C.RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{C.GREY}bye{C.RESET}")
                break

            if not line:
                continue
            if line in ("/exit", "/quit", "exit", "quit"):
                print(f"{C.GREY}bye{C.RESET}")
                break
            if line == "/help":
                self.print_help()
                continue
            if line == "/state":
                self.print_state()
                continue
            if line == "/audit":
                self.print_audit()
                continue
            if line.startswith("/audit"):
                parts = line.split()
                limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
                self.print_audit(limit)
                continue
            if line == "/ledger":
                self.print_full_ledger()
                continue
            if line == "/session":
                # 重开一个
                cs_id = self.store.create_session(self.user_id, {"current_stage": "NEW"})["creative_session_id"]
                self.session_id = cs_id
                print(f"{C.GREEN}✓ new session: {cs_id}{C.RESET}")
                continue

            self.process_input(line)


def main():
    parser = argparse.ArgumentParser(description="本地 chat REPL 验证 agent")
    parser.add_argument("--user-id", default="u_demo")
    parser.add_argument("--chat-id", default="oc_demo")
    parser.add_argument("--auto", default="hi", help="启动时自动发的第一条消息")
    parser.add_argument("--no-auto", action="store_true", help="不自动发")
    parser.add_argument("--db", default=None, help="SQLite db 路径(可选)")
    args = parser.parse_args()

    repl = ChatRepl(
        user_id=args.user_id,
        chat_id=args.chat_id,
        db_path=args.db,
    )
    if not args.no_auto and args.auto:
        repl.process_input(args.auto)
    repl.run()


if __name__ == "__main__":
    main()
