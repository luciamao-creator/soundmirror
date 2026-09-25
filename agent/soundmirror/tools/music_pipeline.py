"""
Music Pipeline — 一站式音乐生成 + 剪裁 + 上传

完整链路:
  1. Matrix.generate(prompt, lyrics) → node_id
  2. Matrix.get_audio_url(node_id) → 2-day signed URL
  3. AudioClipper.clip_from_url(...) → 30s preview 本地文件
  4. AssetUploader.upload_file(...) → 1-hour shareable URL

如果 mcode-tools 未登录,自动降级到 mock(返回 mock_node_* + mock URL)

2026-09-19: 加 runtime fallback(PM P1-1 Day 2)
  - fallback_policy:
      real_required  失败就明确告知,不伪装
      fallback_allowed 可降级 Mock,UI 标"测试音频"
      mock_only  只用于内部链路验证
  - URL 刷新:
      upload_result.url_expires_in 提供 URL 有效秒数
      refresh_url(local_path) 重新调 uploader.upload_file
"""
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .contracts import MusicResult, AssetUploadResult
from .matrix_adapter import MatrixMusicAdapter, MockMusicAdapter, MatrixConnectionError
from .audio_clipper import FfmpegAudioClipper, MockAudioClipper
from .asset_uploader import MatrixAssetUploader, MockAssetUploader
import shutil


# ============================================================
#  Fallback Policy — PM P1-1
# ============================================================

FALLBACK_POLICY_REAL_REQUIRED = "real_required"
FALLBACK_POLICY_ALLOWED = "fallback_allowed"
FALLBACK_POLICY_MOCK_ONLY = "mock_only"

VALID_POLICIES = {
    FALLBACK_POLICY_REAL_REQUIRED,
    FALLBACK_POLICY_ALLOWED,
    FALLBACK_POLICY_MOCK_ONLY,
}


class MusicPipeline:
    """End-to-end music generation + processing"""

    def __init__(
        self,
        music_gen=None,
        clipper=None,
        uploader=None,
        prefer_real: bool = True,
        fallback_policy: str = FALLBACK_POLICY_ALLOWED,
    ):
        """
        Args:
            prefer_real: True 时优先用真实实现,失败降级 mock(只用于构造时)
            fallback_policy:
                real_required  失败就抛错,不降级
                fallback_allowed 运行时 fallback to Mock(默认)
                mock_only 强制 Mock(测试)
        """
        if fallback_policy not in VALID_POLICIES:
            raise ValueError(
                f"Invalid fallback_policy: {fallback_policy}, "
                f"valid: {sorted(VALID_POLICIES)}"
            )
        self.fallback_policy = fallback_policy
        self.prefer_real = prefer_real

        # mock_only 模式强制走 mock,忽略传入组件
        if fallback_policy == FALLBACK_POLICY_MOCK_ONLY:
            self.music_gen = MockMusicAdapter()
            self.clipper = MockAudioClipper()
            self.uploader = MockAssetUploader()
        else:
            self.music_gen = music_gen or self._make_music()
            self.clipper = clipper or self._make_clipper()
            self.uploader = uploader or self._make_uploader()

    def _make_music(self):
        if not self.prefer_real:
            return MockMusicAdapter()
        try:
            real = MatrixMusicAdapter()
            real.validate_schema()
            return real
        except MatrixConnectionError:
            return MockMusicAdapter()

    def _make_clipper(self):
        if not self.prefer_real:
            return MockAudioClipper()
        if shutil.which("ffmpeg"):
            return FfmpegAudioClipper()
        return MockAudioClipper()

    def _make_uploader(self):
        if not self.prefer_real:
            return MockAssetUploader()
        return MatrixAssetUploader()

    def _swap_to_mock(self):
        """runtime fallback:把三件套切到 Mock 集合"""
        self.music_gen = MockMusicAdapter()
        self.clipper = MockAudioClipper()
        self.uploader = MockAssetUploader()

    def generate_full_song(
        self,
        prompt: str,
        lyrics: str = "",
        bitrate: int = 128000,
        sample_rate: int = 44100,
    ) -> MusicResult:
        """生成完整歌曲(M1:这是用户拿到的第一次产物)

        2026-09-19 改(PM P1-1):
          - 包装 try/except:异常时按 fallback_policy 决定降级或抛错
          - 降级时自动 swap 到 MockMusicAdapter,后续调用不再尝试真实现
          - MusicResult.fallback_from 字段记录降级原因
        """
        try:
            result = self.music_gen.generate(
                prompt=prompt,
                lyrics=lyrics,
                bitrate=bitrate,
                sample_rate=sample_rate,
            )
            if not result.success:
                # MusicResult.success=False 视为失败
                if self.fallback_policy == FALLBACK_POLICY_ALLOWED:
                    self._swap_to_mock()
                    result = self.music_gen.generate(
                        prompt=prompt, lyrics=lyrics,
                        bitrate=bitrate, sample_rate=sample_rate,
                    )
                    result.fallback_from = "music_gen_returned_failure"
                # real_required / mock_only: 保持失败返回
            return result
        except Exception as e:
            if self.fallback_policy == FALLBACK_POLICY_ALLOWED:
                self._swap_to_mock()
                result = self.music_gen.generate(
                    prompt=prompt, lyrics=lyrics,
                    bitrate=bitrate, sample_rate=sample_rate,
                )
                result.fallback_from = f"music_gen_exception:{type(e).__name__}:{e}"
                return result
            # real_required: 透传异常
            raise

    def generate_with_preview(
        self,
        prompt: str,
        lyrics: str = "",
        output_dir: str = "./out/audio",
        preview_seconds: int = 30,
        bitrate: int = 128000,
        sample_rate: int = 44100,
    ) -> dict:
        """完整流程:生成 + 剪 30s 预览 + 上传

        Returns dict {
          music: MusicResult,
          preview_path: Optional[str],
          preview_url: Optional[str],
          uploaded: bool,
          error: Optional[str],
          fallback_from: Optional[str],  # 新增:记录降级原因
          url_expires_at: Optional[str], # 新增:URL 过期时间(UTC ISO)
        }
        """
        fallback_reason = None

        # T1: 生成(含 runtime fallback)
        # real_required 时透传异常;fallback_allowed / mock_only 时返回 fail dict
        propagate = (self.fallback_policy == FALLBACK_POLICY_REAL_REQUIRED)
        try:
            result = self.generate_full_song(
                prompt=prompt, lyrics=lyrics,
                bitrate=bitrate, sample_rate=sample_rate,
            )
            if not result.success:
                return self._fail_dict(result, result.error or "music_gen failed")
            if hasattr(result, "fallback_from") and result.fallback_from:
                fallback_reason = result.fallback_from
        except Exception as e:
            if propagate:
                # real_required:异常透传给上层
                raise
            return self._fail_dict(
                MusicResult(success=False, error=str(e), node_id="", audio_url="", model=""),
                f"generate exception: {e}",
            )

        # T2: 剪 30s
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        # 2026-09-19 改:用完整 node_id(不再截断 12 位,避免 B/C 不同 node 截到相同前缀)
        # 用 / 替换 : 避免路径分隔符冲突(实际 node_id 不含 /, 但保险)
        node_short = (result.node_id or "unknown").replace("/", "_")
        preview_path = str(Path(output_dir) / f"{node_short}_preview.mp3")

        try:
            if result.audio_url:
                ok = self.clipper.clip_from_url(
                    result.audio_url, preview_path,
                    duration_seconds=preview_seconds,
                )
            else:
                ok = False
            if not ok:
                return self._fail_dict(result, "clip failed")
        except Exception as e:
            # 剪裁失败:按 fallback_policy 决定
            if self.fallback_policy == FALLBACK_POLICY_ALLOWED:
                self._swap_to_mock()
                # 重新生成(mock 会写真本地文件)
                mock_pipeline_result = MockMusicPipeline._singleton_generate(
                    prompt=prompt, lyrics=lyrics,
                    output_dir=output_dir, preview_seconds=preview_seconds,
                )
                return {
                    "music": result,
                    "preview_path": mock_pipeline_result["preview_path"],
                    "preview_url": mock_pipeline_result["preview_url"],
                    "uploaded": True,
                    "error": None,
                    "fallback_from": f"clip_exception:{type(e).__name__}:{e}",
                    "url_expires_at": _url_expires_at_iso(mock_pipeline_result.get("url_expires_in")),
                }
            return self._fail_dict(result, f"clip exception: {e}")

        # T3: 上传拿 shareable URL
        upload_result = self.uploader.upload_file(preview_path)

        if not upload_result.success:
            # 上传失败 — 仍然有本地文件,只是没 URL
            return {
                "music": result,
                "preview_path": preview_path,
                "preview_url": None,
                "uploaded": False,
                "error": upload_result.error,
                "fallback_from": fallback_reason,
                "url_expires_at": None,
            }

        return {
            "music": result,
            "preview_path": preview_path,
            "preview_url": upload_result.url,
            "uploaded": True,
            "error": None,
            "fallback_from": fallback_reason,
            "url_expires_at": _url_expires_at_iso(upload_result.url_expires_in),
        }

    def generate_full_song_from_node(self, node_id: str, output_dir: str) -> dict:
        """从已有 highlight 的 node_id 拉完整歌曲(不剪)

        2026-09-20 改(补全能力):
          - 之前:_highlight_select 只记选择,完整歌曲生成被跳过
          - 现在:用 node_id 调 music_gen.get_audio_url 拿 full URL
                → 下载到本地 full_song_path
                → uploader.upload_file 拿 1h signed URL
                → 返回完整字段(full_song_path, full_song_url, duration_ms, sha256, expires_at, adapter)
        """
        import subprocess as sp
        import hashlib
        import json as _json

        if not node_id:
            return _empty_full_song_result(node_id=None, error="MISSING_NODE_ID")

        # 1) 拿 audio_url(Matrix asset)
        try:
            audio_url = self.music_gen.get_audio_url(node_id)
        except Exception as e:
            return _empty_full_song_result(node_id=node_id, error=f"get_audio_url exception: {e}")

        if not audio_url:
            return _empty_full_song_result(node_id=node_id, error="MATRIX_RETURNED_NO_AUDIO_URL")

        # 2) 下载完整歌曲
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        node_short = node_id.replace("/", "_")
        full_path = str(Path(output_dir) / f"{node_short}_full.mp3")
        tmp_path = full_path + ".tmp"

        try:
            dl = sp.run(
                ["curl", "-sSL", "--max-time", "120", "-o", tmp_path, audio_url],
                capture_output=True, text=True, timeout=130,
            )
            if dl.returncode != 0 or not Path(tmp_path).exists():
                return _empty_full_song_result(node_id=node_id, error=f"DOWNLOAD_FAILED: {dl.stderr[:200] if dl.stderr else ''}")

            size = Path(tmp_path).stat().st_size
            if size < 1000:
                Path(tmp_path).unlink()
                return _empty_full_song_result(node_id=node_id, error=f"DOWNLOAD_TOO_SMALL ({size}B)")

            # 3) 校验是合法 MP3
            probe = sp.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_streams", "-show_format", tmp_path],
                capture_output=True, text=True, timeout=10,
            )
            if probe.returncode != 0:
                Path(tmp_path).unlink()
                return _empty_full_song_result(node_id=node_id, error=f"FFPROBE_FAILED: {probe.stderr[:200]}")

            probe_data = _json.loads(probe.stdout)
            streams = probe_data.get("streams", [])
            fmt = probe_data.get("format", {})
            if not any(s.get("codec_name") == "mp3" for s in streams):
                Path(tmp_path).unlink()
                return _empty_full_song_result(node_id=node_id, error="NOT_MP3")

            duration_ms = int(float(fmt.get("duration", 0)) * 1000)

            # 4) sha256
            sha256 = hashlib.sha256()
            with open(tmp_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256.update(chunk)
            sha256_hex = sha256.hexdigest()

            # 5) 原子 rename 到最终路径
            if Path(full_path).exists():
                Path(full_path).unlink()
            Path(tmp_path).rename(full_path)

        except subprocess.TimeoutExpired:
            _safe_remove(tmp_path)
            return _empty_full_song_result(node_id=node_id, error="DOWNLOAD_TIMEOUT")
        except Exception as e:
            _safe_remove(tmp_path)
            return _empty_full_song_result(node_id=node_id, error=f"EXCEPTION: {e}")

        # 6) 上传到 OSS 拿 1h signed URL
        try:
            upload_result = self.uploader.upload_file(full_path)
        except Exception as e:
            upload_result = None
            upload_error = str(e)
        else:
            upload_error = None

        result = {
            "node_id": node_id,
            "full_song_path": full_path,
            "full_song_url": upload_result.url if upload_result and upload_result.success else None,
            "uploaded": upload_result.success if upload_result else False,
            "duration_ms": duration_ms,
            "bytes": Path(full_path).stat().st_size,
            "checksum_sha256": sha256_hex,
            "mime_type": "audio/mpeg",
            "adapter": type(self.music_gen).__name__,
            "signed_url_expires_at": _url_expires_at_iso(upload_result.url_expires_in) if upload_result and upload_result.success else None,
            "error": None if upload_result and upload_result.success else (upload_error or upload_result.error if upload_result else "upload_unknown"),
        }
        return result

    @staticmethod
    def _fail_dict(result: MusicResult, error: str) -> dict:
        return {
            "music": result,
            "preview_path": None,
            "preview_url": None,
            "uploaded": False,
            "error": error,
            "fallback_from": None,
            "url_expires_at": None,
        }

    def refresh_url(self, local_path: str) -> Optional[str]:
        """URL 过期后重新生成签名 URL(PM P1-3)

        调用 uploader.upload_file 重传同一文件 → 新 1h signed URL。
        注意:本方法会真正走 OSS 一次,可能产生存储成本。
        """
        if not os.path.exists(local_path):
            return None
        result = self.uploader.upload_file(local_path)
        return result.url if result.success else None


# ============================================================
#  URL 过期时间 helper
# ============================================================

def _url_expires_at_iso(seconds: Optional[int]) -> Optional[str]:
    """从"剩余秒数"算 UTC ISO-8601 过期时间"""
    if not seconds or seconds <= 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).isoformat(timespec="seconds")


def _empty_full_song_result(node_id=None, error="UNKNOWN"):
    """空结果(所有字段就位)— 用于 early return 路径"""
    return {
        "node_id": node_id,
        "full_song_path": None,
        "full_song_url": None,
        "uploaded": False,
        "duration_ms": None,
        "bytes": None,
        "checksum_sha256": None,
        "mime_type": "audio/mpeg",
        "adapter": None,
        "signed_url_expires_at": None,
        "error": error,
    }


class MockMusicPipeline:
    """Memory mock — 测试用

    2026-09-18 改:写真静音 MP3(ffmpeg nullsrc)而非 22 字节文本。
    现在 produce 的 .mp3 文件可以被播放器识别、有正确时长、可真上传 OSS。
    """

    def __init__(self):
        self.generated = []
        self._counter = 0

    @staticmethod
    def _write_silent_mp3(path: str, duration: int = 30,
                          sample_rate: int = 44100, bitrate: int = 128000,
                          channels: int = 2) -> int:
        """写真静音 MP3(ffmpeg nullsrc anullsrc)

        2026-09-19 改:原子保护 + 完整性校验
        - 写临时文件 .tmp → ffmpeg 成功 → 原子 rename
        - 校验:returncode + 输出文件存在 + 非空 + ffprobe 验证
        - ffmpeg stderr 不长于 2KB 时完整透传,否则截断
        - 失败时清理临时文件

        Returns 文件字节数;失败抛 RuntimeError
        """
        import subprocess
        import shutil

        path = str(path)
        tmp_path = path + ".tmp"

        # 清理任何残留 tmp
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-nostdin",
            "-i", f"anullsrc=channel_layout={'stereo' if channels == 2 else 'mono'}:sample_rate={sample_rate}",
            "-t", str(duration),
            "-ac", str(channels),
            "-ar", str(sample_rate),
            "-b:a", str(bitrate),
            "-f", "mp3",
            tmp_path,
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=20,
            )
        except subprocess.TimeoutExpired:
            _safe_remove(tmp_path)
            raise RuntimeError(f"ffmpeg timeout writing {path}")
        except FileNotFoundError:
            _safe_remove(tmp_path)
            raise RuntimeError("ffmpeg not installed (apt-get install ffmpeg)")

        # 1) 校验 returncode
        if proc.returncode != 0:
            stderr_snip = proc.stderr[-2000:] if proc.stderr else "(no stderr)"
            _safe_remove(tmp_path)
            raise RuntimeError(f"ffmpeg exit={proc.returncode}: {stderr_snip}")

        # 2) 校验文件存在
        if not os.path.exists(tmp_path):
            raise RuntimeError(f"ffmpeg produced no output at {tmp_path}")

        # 3) 校验文件非空
        try:
            size = os.path.getsize(tmp_path)
        except OSError as e:
            _safe_remove(tmp_path)
            raise RuntimeError(f"ffmpeg output unreadable: {e}")

        if size == 0:
            _safe_remove(tmp_path)
            raise RuntimeError(f"ffmpeg produced 0-byte output at {tmp_path}")

        # 4) ffprobe 契约校验(codec/duration 必须正确)
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_streams", "-show_format", tmp_path],
                capture_output=True, text=True, timeout=10,
            )
            if probe.returncode != 0:
                _safe_remove(tmp_path)
                raise RuntimeError(f"ffprobe failed: {probe.stderr[:300]}")
            import json as _json
            probe_data = _json.loads(probe.stdout)
            streams = probe_data.get("streams", [])
            fmt = probe_data.get("format", {})
            # 校验 codec=mp3
            if not any(s.get("codec_name") == "mp3" for s in streams):
                _safe_remove(tmp_path)
                raise RuntimeError(
                    f"ffprobe: codec != mp3, got {[s.get('codec_name') for s in streams]}"
                )
            # 校验 duration (允许 ±0.2s 误差)
            actual_dur = float(fmt.get("duration", 0))
            if abs(actual_dur - duration) > 0.2:
                _safe_remove(tmp_path)
                raise RuntimeError(
                    f"ffprobe: duration mismatch: expected={duration}s actual={actual_dur}s"
                )
            # 校验 channels
            if streams:
                actual_ch = streams[0].get("channels")
                if actual_ch != channels:
                    _safe_remove(tmp_path)
                    raise RuntimeError(
                        f"ffprobe: channels mismatch: expected={channels} actual={actual_ch}"
                    )
        except (subprocess.TimeoutExpired, _json.JSONDecodeError, KeyError) as e:
            _safe_remove(tmp_path)
            raise RuntimeError(f"ffprobe contract check failed: {e}")

        # 5) 原子 rename
        try:
            # 如果目标文件已存在(并发),先清掉
            if os.path.exists(path):
                os.remove(path)
            shutil.move(tmp_path, path)
        except OSError as e:
            _safe_remove(tmp_path)
            raise RuntimeError(f"atomic rename failed: {e}")

        return size

    def generate_full_song_from_node(self, node_id, output_dir):
        """Mock 没有真实 node_id 资产,直接返"无完整歌曲"错误

        2026-09-20 加:为了让 _highlight_select 在 mock 模式下不报错
        """
        return {
            "node_id": node_id,
            "full_song_path": None,
            "full_song_url": None,
            "uploaded": False,
            "duration_ms": None,
            "bytes": None,
            "checksum_sha256": None,
            "mime_type": "audio/mpeg",
            "adapter": "MockMusicPipeline",
            "signed_url_expires_at": None,
            "error": "MOCK_NO_REAL_ASSET",
        }

    def generate_with_preview(self, prompt, lyrics="", output_dir="./out/audio",
                              preview_seconds=30, bitrate=128000, sample_rate=44100,
                              format="mp3", **kwargs):
        """生成 + 写本地 30s 预览文件 + 模拟上传

        Returns: dict { music, preview_path, preview_url, uploaded, bytes, error }
        """
        self._counter += 1
        node_id = f"mock_node_{self._counter:08d}"

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        preview_path = str(Path(output_dir) / f"{node_id}_preview.{format}")

        # 写真静音 MP3 / WAV(走原子保护 + ffprobe 契约)
        bytes_written = self._write_silent_mp3(
            preview_path,
            duration=preview_seconds,
            sample_rate=sample_rate,
            bitrate=bitrate,
        )

        music = MusicResult(
            success=True,
            node_id=node_id,
            file_name=f"{node_id}.{format}",
            audio_url=f"https://mock.local/{node_id}.{format}",
            model="mock-pipeline",
        )

        self.generated.append({
            "prompt": prompt[:50],
            "lyrics_len": len(lyrics),
            "node_id": node_id,
            "preview_path": preview_path,
            "bytes": bytes_written,
            "duration": preview_seconds,
        })

        return {
            "music": music,
            "preview_path": preview_path,
            "preview_url": f"https://mock.local/preview_{node_id}.{format}",
            "uploaded": True,
            "bytes": bytes_written,
            "error": None,
        }

    def generate_full_song(self, prompt, lyrics="", **kwargs):
        """生成完整歌曲(M1 mock 只返回 node_id)"""
        self._counter += 1
        return MusicResult(
            success=True,
            node_id=f"mock_full_{self._counter:08d}",
            audio_url=f"https://mock.local/full_{self._counter}.mp3",
            model="mock-pipeline",
        )

    @staticmethod
    def _singleton_generate(prompt, lyrics="", output_dir="./out/audio",
                            preview_seconds=30, bitrate=128000, sample_rate=44100,
                            **kwargs):
        """Singleton 入口:不传 self 也能调(供 runtime fallback 使用)

        Returns: dict { music, preview_path, preview_url, uploaded, bytes, error, url_expires_in }
        """
        s = MockMusicPipeline()
        result = s.generate_with_preview(
            prompt=prompt, lyrics=lyrics,
            output_dir=output_dir, preview_seconds=preview_seconds,
            bitrate=bitrate, sample_rate=sample_rate, **kwargs,
        )
        # mock url 永久有效(模拟 1h)
        result["url_expires_in"] = 3600
        return result


def _safe_remove(path: str) -> None:
    """清理半成品临时文件 — 失败不抛"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass

