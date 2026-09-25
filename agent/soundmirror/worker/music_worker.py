"""
Music Worker — 处理 music.generate tool_request

完整流程:
  1. 收 job (job_type="music.generate", payload={prompt, lyrics, ...})
  2. 调 MusicPipeline.generate_with_preview()
  3. 把结果(本地 30s 文件 + 1h URL)写回 ledger
  4. 返回 result
"""
from typing import Optional

from .base import Worker, Job
from ..tools.music_pipeline import MusicPipeline, MockMusicPipeline


class MusicWorker(Worker):
    job_type = "music.generate"

    def __init__(self, pipeline: Optional[object] = None, output_dir: str = "./out/audio"):
        super().__init__(name="music.generate")
        self.pipeline = pipeline or self._default_pipeline()
        self.output_dir = output_dir

    def _default_pipeline(self):
        return MusicPipeline(prefer_real=True)

    def handle_job(self, job: Job) -> dict:
        """Process music.generate"""
        prompt = job.payload.get("prompt", "")
        lyrics = job.payload.get("lyrics", "")
        bitrate = job.payload.get("bitrate", 128000)
        sample_rate = job.payload.get("sample_rate", 44100)
        preview_seconds = job.payload.get("preview_seconds", 30)

        if not prompt:
            raise ValueError("payload.prompt is required")
        if len(prompt) < 10:
            raise ValueError(f"prompt too short ({len(prompt)} chars, need >= 10)")
        if lyrics and not (10 <= len(lyrics) <= 600):
            raise ValueError(f"lyrics length {len(lyrics)} out of range [10, 600]")

        # 生成 + 剪裁 + 上传
        result = self.pipeline.generate_with_preview(
            prompt=prompt,
            lyrics=lyrics,
            output_dir=self.output_dir,
            preview_seconds=preview_seconds,
            bitrate=bitrate,
            sample_rate=sample_rate,
        )

        if not result["music"].success:
            raise RuntimeError(f"music generation failed: {result['music'].error}")

        return {
            "node_id": result["music"].node_id,
            "file_name": result["music"].file_name,
            "audio_url": result["music"].audio_url,
            "model": result["music"].model,
            "preview_path": result.get("preview_path"),
            "preview_url": result.get("preview_url"),
            "uploaded": result.get("uploaded", False),
        }


class RevisionWorker(Worker):
    """revision.request 触发的整首重生成 worker

    Job payload: {feedback, current_node_id}
    Result: 新 node_id + preview + url
    """
    job_type = "revision.generate"

    def __init__(self, pipeline: Optional[object] = None, output_dir: str = "./out/audio"):
        super().__init__(name="revision.generate")
        self.pipeline = pipeline or MusicPipeline(prefer_real=True)
        self.output_dir = output_dir

    def handle_job(self, job: Job) -> dict:
        prompt = job.payload.get("prompt", "")
        lyrics = job.payload.get("lyrics", "")
        feedback = job.payload.get("feedback", "")
        if feedback and not prompt:
            # 用户只给反馈,没新的 prompt → 用 feedback 当 prompt(简单处理)
            prompt = f"Re-create the song with this user feedback in mind: {feedback}"

        result = self.pipeline.generate_with_preview(
            prompt=prompt or "acoustic folk ballad",
            lyrics=lyrics,
            output_dir=self.output_dir,
            preview_seconds=30,
        )
        if not result["music"].success:
            raise RuntimeError(f"revision generation failed: {result['music'].error}")

        return {
            "node_id": result["music"].node_id,
            "audio_url": result["music"].audio_url,
            "preview_url": result.get("preview_url"),
            "feedback_used": feedback,
        }
