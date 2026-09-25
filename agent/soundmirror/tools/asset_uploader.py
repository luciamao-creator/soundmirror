"""
Asset Uploader — 上传本地文件,拿到短期可分享 URL

通过 mcode-tools upload-temp-url:
  - 输入:本地文件路径
  - 输出:1 小时的 HTTPS URL(matrix 签名)
  - 用途:让用户能听 matrix 生成的音频(play 按钮)

限制:
  - 单文件 ≤ 200 MB(matrix quota)
  - URL 1 小时过期,过期要重新上传(PM P1-3 用 refresh_url 重传)
"""
import json
import subprocess
from pathlib import Path
from typing import Optional
from .contracts import AssetUploader, AssetUploadResult


class MatrixAssetUploader:
    """通过 mcode-tools 上传资产"""

    def __init__(self, mcode_bin: str = "mcode-tools", timeout_seconds: int = 60):
        self.mcode_bin = mcode_bin
        self.timeout = timeout_seconds

    def upload_file(self, local_path: str) -> AssetUploadResult:
        """上传本地文件,返回 1-hour HTTPS URL

        2026-09-19 改(PM P1-3):同时返回 url_expires_in 和 object_key
        """
        if not Path(local_path).exists():
            return AssetUploadResult(success=False, error=f"file not found: {local_path}")
        if Path(local_path).stat().st_size == 0:
            return AssetUploadResult(success=False, error="empty file")

        try:
            proc = subprocess.run(
                [self.mcode_bin, "upload-temp-url", local_path],
                capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return AssetUploadResult(success=False, error=f"upload timeout after {self.timeout}s")

        if proc.returncode != 0:
            return AssetUploadResult(
                success=False,
                error=f"upload failed: {proc.stderr or proc.stdout}",
            )

        # 解析输出 — mcode-tools 返回 JSON(格式可能逐版变化)
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # 如果是裸字符串
            url = proc.stdout.strip()
            if url.startswith("http"):
                return AssetUploadResult(
                    success=True, url=url,
                    url_expires_in=3600,  # 默认 1h
                    object_key=local_path,  # 用本地路径当 key
                )
            return AssetUploadResult(success=False, error=f"unexpected output: {proc.stdout[:200]}")

        # 兼容多种字段名
        url = (
            data.get("url")
            or data.get("temp_url")
            or data.get("tempUrl")
            or data.get("public_url")
            or data.get("publicUrl")
            or data.get("data", {}).get("url")
            or data.get("data", {}).get("temp_url")
        )
        node_id = (
            data.get("node_id") or data.get("nodeId")
            or data.get("data", {}).get("node_id")
        )
        # PM P1-3: 优先从响应里拿过期秒数,默认 3600
        url_expires_in = (
            data.get("url_expires_in")
            or data.get("urlExpiresIn")
            or data.get("expires_in")
            or data.get("data", {}).get("url_expires_in")
            or 3600
        )
        # PM P1-3: 稳定 object_key(用于 refresh)
        object_key = (
            data.get("object_key") or data.get("objectKey")
            or data.get("key") or data.get("data", {}).get("object_key")
            or node_id or local_path
        )

        if not url:
            return AssetUploadResult(
                success=False,
                error=f"no url in response: {data}",
            )
        return AssetUploadResult(
            success=True, url=url, node_id=node_id,
            url_expires_in=url_expires_in, object_key=object_key,
        )

    def upload_bytes(self, data: bytes, file_name: str) -> AssetUploadResult:
        """先写到临时文件再上传"""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=f"_{file_name}", delete=False) as f:
            f.write(data)
            tmp_path = f.name
        try:
            result = self.upload_file(tmp_path)
        finally:
            try:
                Path(tmp_path).unlink()
            except Exception:
                pass
        return result


class MockAssetUploader:
    """测试用"""

    def __init__(self, default_expires_in: int = 3600):
        self.calls = []
        self._counter = 0
        self.default_expires_in = default_expires_in

    def upload_file(self, local_path):
        self._counter += 1
        url = f"https://mock.local/upload_{self._counter}.tmp"
        self.calls.append({"method": "upload_file", "input": local_path, "url": url})
        return AssetUploadResult(
            success=True, url=url,
            node_id=f"mock_up_{self._counter:08d}",
            url_expires_in=self.default_expires_in,
            object_key=f"mock_key_{self._counter:08d}",
        )

    def upload_bytes(self, data, file_name):
        self._counter += 1
        url = f"https://mock.local/upload_{self._counter}_{file_name}"
        self.calls.append({"method": "upload_bytes", "name": file_name, "size": len(data), "url": url})
        return AssetUploadResult(
            success=True, url=url,
            url_expires_in=self.default_expires_in,
            object_key=f"mock_key_{self._counter:08d}",
        )
