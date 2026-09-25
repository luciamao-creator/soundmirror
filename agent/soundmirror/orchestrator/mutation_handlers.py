"""
Orchestrator mutation handlers — 让按钮点下去真的推进 stage(不只是装饰)

2026-09-22 改(Bridge E2E 验证发现):
  - 旧版只在 chat_repl.py 里注册这些 handler
  - run_webhook.py / cron_bridge.py 没注册 → 真实 Lark 按钮事件落到 gateway
    时 orchestrator.handlers 是空的,action 全 noop → stage 不推进
  - 现在:把 handlers 抽到此处,run_webhook.py / chat_repl.py 都从这里 build
  - 后续:也可被 gateway 的 _handle_action 直接调用,不绕 orchestrator(简化)
"""
import os
import uuid
from datetime import datetime, timedelta, timezone


def _url_expires_in_1h():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")


def _sha256_of_file(path):
    import hashlib
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def build_mutation_handlers(state_store):
    """返回 orchestrator 的 mutation_handlers dict。"""
    handlers = {}

    # ---------- ARCHAEOLOGY ----------
    def _archaeology_confirm(state_store, cs_id, payload, actor_user_id):
        """点击'够了' → 切到 IDENTITY_FITTING + 生成 3 个身份候选"""
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
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

    handlers["archaeology.confirm"] = _archaeology_confirm

    # ---------- IDENTITY ----------
    def _identity_select(state_store, cs_id, payload, actor_user_id, action="keep"):
        """identity.keep / identity.borrow / identity.reject → 写 persona_selections"""
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
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

    def _identity_keep(state_store, cs_id, payload, actor_user_id):
        _identity_select(state_store, cs_id, payload, actor_user_id, action="keep")

    def _identity_borrow(state_store, cs_id, payload, actor_user_id):
        _identity_select(state_store, cs_id, payload, actor_user_id, action="borrow")

    def _identity_reject(state_store, cs_id, payload, actor_user_id):
        _identity_select(state_store, cs_id, payload, actor_user_id, action="reject")

    handlers["identity.keep"] = _identity_keep
    handlers["identity.borrow"] = _identity_borrow
    handlers["identity.reject"] = _identity_reject

    # ---------- VARIABLE ----------
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

    def _variable_confirm(state_store, cs_id, payload, actor_user_id):
        """variable.confirm → 进 PROPOSAL_REVIEW(显示歌词提案)
        歌词从 archaeology_facts 拼出(简化)
        """
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        music_spec = session["ledger"].get("music_spec", {})
        facts = session["ledger"].get("archaeology_facts", [])
        verse_lines = []
        for f in facts[:2]:
            text = f.get("text", "") if isinstance(f, dict) else str(f)
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

    handlers["variable.change"] = _variable_set
    handlers["variable.confirm"] = _variable_confirm

    # ---------- PROPOSAL(生成 3 个候选) ----------
    def _proposal_confirm(state_store, cs_id, payload, actor_user_id):
        """proposal.confirm → 触发生成 3 个候选 → 进 HIGHLIGHT_REVIEW"""
        use_real_matrix = os.environ.get("SOUNDMIRROR_USE_REAL_MATRIX", "0") == "1"
        if use_real_matrix:
            from ..tools.music_pipeline import MusicPipeline, FALLBACK_POLICY_REAL_REQUIRED
            from ..tools.matrix_adapter import MatrixMusicAdapter
            real_adapter = MatrixMusicAdapter(timeout_seconds=180)
            pipeline = MusicPipeline(
                music_gen=real_adapter,
                prefer_real=True,
                fallback_policy=FALLBACK_POLICY_REAL_REQUIRED,
            )
            adapter_name = "MusicPipeline(real_matrix, real_required, timeout=180s)"
        else:
            from ..tools import MockMusicPipeline
            pipeline = MockMusicPipeline()
            adapter_name = "MockMusicPipeline"

        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        existing_job = session["ledger"].get("generation_job_id")
        existing_status = session["ledger"].get("generation_status")
        existing_clips = session["ledger"].get("highlight_clips", [])

        # 2026-09-22 改(Bridge E2E Failure 2):
        #   幂等条件:已有 job + status=READY + 至少 1 个 clip READY
        #   旧逻辑:status in (READY, GENERATING) → 幂等
        #     漏洞:GENERATING 可能是前进程死前的"声明已开始",clips 仍 PENDING
        #           bridge 接管时按幂等跳过 → 永久卡在 PENDING
        #   现在:clips 全 PENDING(说明前进程死前没生成任一首)→ 强制重新生成
        all_clips_pending = (
            bool(existing_clips)
            and all(c.get("status") == "PENDING" for c in existing_clips)
        )
        any_clip_ready = any(c.get("status") == "READY" for c in existing_clips)

        if (existing_job
            and existing_status == "READY"
            and any_clip_ready):
            # 真的生成过了 — 幂等
            state_store.apply_mutation(cs_id, {
                "expected_revision": rev,
                "operations": [
                    {"op": "set", "path": "/current_stage", "value": "HIGHLIGHT_REVIEW"},
                ],
            }, actor_user_id)
            return
        # 否则:重新生成(clips 可能是 PENDING 孤儿 或 全新请求)
        if existing_job and all_clips_pending:
            print(f"  [Mavis] retry orphan generation: job={existing_job} clips 全 PENDING",
                  flush=True)

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
                {"op": "set", "path": "/highlight_clips", "value": pending_clips},
                {"op": "set", "path": "/current_stage", "value": "HIGHLIGHT_REVIEW"},
            ],
        }, actor_user_id)

        music_spec = session["ledger"].get("music_spec", {})
        proposal = session["ledger"].get("current_proposal", {})
        lyrics = (proposal.get("lyrics", "") if proposal else "")
        if not lyrics:
            lyrics = "[verse]\n海风停了,傍晚的光\n[chorus]\n你不在 但影子还在\n"

        out_dir = f"/workspace/voice-persona-assessment/agent/soundmirror/out/audio_highlight/{cs_id}"
        os.makedirs(out_dir, exist_ok=True)

        has_chinese = any('\u4e00' <= c <= '\u9fff' for c in lyrics)
        if has_chinese:
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
            slot_suffix = slot_label
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

        sess2 = state_store.get_session(cs_id)
        new_rev = sess2["state_revision"]
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

    handlers["proposal.confirm"] = _proposal_confirm

    # ---------- HIGHLIGHT(选择) + FULL SONG ----------
    def _highlight_select(state_store, cs_id, payload, actor_user_id):
        """clip.select / highlight.select → 校验 + 选 + 自动生成完整歌曲"""
        slot = payload.get("slot", "A")
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        gen_status = session["ledger"].get("generation_status", "")
        if gen_status not in ("READY", "READY_WITH_FAILURES"):
            raise RuntimeError(f"generation not ready (status={gen_status})")
        clips = session["ledger"].get("highlight_clips", [])
        clip = next((c for c in clips if c.get("slot") == slot), None)
        if not clip or clip.get("status") != "READY":
            raise RuntimeError(f"slot {slot} not ready")
        node_id = clip.get("node_id")
        is_mock = clip.get("is_mock", True)

        # 1) 选 highlight
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/selected_highlight", "value": {
                    "slot": slot,
                    "node_id": node_id,
                    "preview_path": clip.get("preview_path"),
                    "url": clip.get("url"),
                    "duration_ms": clip.get("duration_ms"),
                    "selected_at": datetime.now(timezone.utc).isoformat(),
                    "is_mock": is_mock,
                }},
                {"op": "set", "path": "/current_stage", "value": "FULL_REVIEW"},
            ],
        }, actor_user_id)

        # 2) 同步生成完整歌曲(测试用,M2 改异步)
        try:
            from ..tools.music_pipeline import MusicPipeline
            use_real_matrix = os.environ.get("SOUNDMIRROR_USE_REAL_MATRIX", "0") == "1"
            if use_real_matrix:
                from ..tools.music_pipeline import FALLBACK_POLICY_REAL_REQUIRED
                from ..tools.matrix_adapter import MatrixMusicAdapter
                real_adapter = MatrixMusicAdapter(timeout_seconds=180)
                pipeline = MusicPipeline(
                    music_gen=real_adapter,
                    prefer_real=True,
                    fallback_policy=FALLBACK_POLICY_REAL_REQUIRED,
                )
            else:
                from ..tools import MockMusicPipeline
                pipeline = MockMusicPipeline()
            out_dir = f"/workspace/voice-persona-assessment/agent/soundmirror/out/audio_full/{cs_id}"
            os.makedirs(out_dir, exist_ok=True)
            full = pipeline.generate_full_song_from_node(
                node_id=node_id,
                is_mock=is_mock,
                output_dir=out_dir,
            )
            if full and full.get("uploaded"):
                sess2 = state_store.get_session(cs_id)
                state_store.apply_mutation(cs_id, {
                    "expected_revision": sess2["state_revision"],
                    "operations": [
                        {"op": "set", "path": "/selected_highlight/full_song_path",
                         "value": full.get("full_song_path")},
                        {"op": "set", "path": "/selected_highlight/full_song_url",
                         "value": full.get("full_song_url")},
                        {"op": "set", "path": "/selected_highlight/full_song_duration_ms",
                         "value": full.get("duration_ms")},
                        {"op": "set", "path": "/selected_highlight/full_song_sha256",
                         "value": _sha256_of_file(full.get("full_song_path"))},
                        {"op": "set", "path": "/selected_highlight/full_song_node_id",
                         "value": full.get("full_song_node_id")},
                    ],
                }, actor_user_id)
        except Exception as e:
            # 完整歌曲生成失败不影响 highlight 选择
            print(f"  [Mavis] full song generation failed: {e}", flush=True)

    handlers["highlight.select"] = _highlight_select
    handlers["clip.select"] = _highlight_select

    # ---------- REVIEW / FULL / PROFILE ----------
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
    handlers["review.approve"] = _review_approve

    def _full_confirm(state_store, cs_id, payload, actor_user_id):
        """full.confirm → 进 ARCHIVE 阶段(完整歌曲已生成,用户确认存档)"""
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/current_stage", "value": "ARCHIVE"},
            ],
        }, actor_user_id)
    handlers["full.confirm"] = _full_confirm

    def _profile_archive(state_store, cs_id, payload, actor_user_id):
        """profile.archive → 写 personal_style_profile + 进 DONE"""
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        profile = {
            "archaeology_facts": session["ledger"].get("archaeology_facts", []),
            "persona_selections": session["ledger"].get("persona_selections", []),
            "music_spec": session["ledger"].get("music_spec", {}),
            "selected_highlight": session["ledger"].get("selected_highlight", {}),
            "archived_at": datetime.now(timezone.utc).isoformat(),
        }
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/personal_style_profile", "value": profile},
                {"op": "set", "path": "/current_stage", "value": "DONE"},
            ],
        }, actor_user_id)
    handlers["profile.archive"] = _profile_archive

    # ---------- STAGE SKIP/BACK ----------
    # 2026-09-22 注:聊天版 chat_repl.py 在 _skip_stage/_stage_back 里用了不存在的
    # NEXT_STAGE/PREV_STAGE 常量(只在 .director 子目录,没真导出)→ ImportError
    # 这里用硬编码顺序保证可用
    _STAGE_FLOW = [
        "INIT", "NEW", "ARCHAEOLOGY", "IDENTITY_FITTING", "VARIABLE_SETUP",
        "PROPOSAL_REVIEW", "HIGHLIGHT_REVIEW", "FULL_REVIEW", "ARCHIVE", "CONFIRMED", "DONE",
    ]

    def _skip_stage(state_store, cs_id, payload, actor_user_id):
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        current = session["ledger"].get("current_stage")
        try:
            i = _STAGE_FLOW.index(current)
            next_stage = _STAGE_FLOW[i + 1] if i + 1 < len(_STAGE_FLOW) else "CONFIRMED"
        except ValueError:
            next_stage = "CONFIRMED"
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/current_stage", "value": next_stage},
            ],
        }, actor_user_id)
    handlers["stage.skip"] = _skip_stage

    def _stage_back(state_store, cs_id, payload, actor_user_id):
        session = state_store.get_session(cs_id)
        rev = session["state_revision"]
        current = session["ledger"].get("current_stage")
        try:
            i = _STAGE_FLOW.index(current)
            prev = _STAGE_FLOW[i - 1] if i > 0 else current
        except ValueError:
            prev = current
        state_store.apply_mutation(cs_id, {
            "expected_revision": rev,
            "operations": [
                {"op": "set", "path": "/current_stage", "value": prev},
            ],
        }, actor_user_id)
    handlers["stage.back"] = _stage_back

    return handlers
