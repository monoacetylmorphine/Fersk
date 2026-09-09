"""Validate downloaded Lark resources before they are written to disk."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Mapping

from fersk_codex.utils.config_loader import CONFIG


ResourceType = Literal["image", "file"]

GENERIC_MIME_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
IMAGE_FORMATS = {"jpeg", "png", "gif", "webp", "bmp"}

FORMAT_EXTENSIONS = {
    "jpeg": ".jpg",
    "png": ".png",
    "gif": ".gif",
    "webp": ".webp",
    "bmp": ".bmp",
    "pdf": ".pdf",
    "mp4": ".mp4",
    "m4a": ".m4a",
    "zip": ".zip",
    "ole": ".bin",
    "json": ".json",
    "text": ".txt",
    "ogg": ".ogg",
    "opus": ".opus",
    "wav": ".wav",
    "mp3": ".mp3",
}

EXTENSION_FORMATS = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".gif": "gif",
    ".webp": "webp",
    ".bmp": "bmp",
    ".pdf": "pdf",
    ".mp4": "mp4",
    ".m4a": "m4a",
    ".zip": "zip",
    ".docx": "zip",
    ".xlsx": "zip",
    ".pptx": "zip",
    ".doc": "ole",
    ".xls": "ole",
    ".ppt": "ole",
    ".json": "json",
    ".txt": "text",
    ".ogg": "ogg",
    ".opus": "opus",
    ".wav": "wav",
    ".mp3": "mp3",
}

MIME_FORMATS = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "application/pdf": "pdf",
    "video/mp4": "mp4",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/msword": "ole",
    "application/vnd.ms-excel": "ole",
    "application/vnd.ms-powerpoint": "ole",
    "application/json": "json",
    "text/json": "json",
    "text/plain": "text",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
}

STRICT_FORMATS = {
    "jpeg", "png", "gif", "webp", "bmp", "pdf", "mp4", "m4a", "zip", "ole",
    "json", "text", "ogg", "opus", "wav", "mp3",
}


class ResourceValidationError(ValueError):
    """Raised when downloaded bytes and their metadata are not trustworthy."""


@dataclass(frozen=True)
class ValidatedResource:
    data: bytes
    file_name: str
    mime_type: str
    detected_format: str
    extension: str


def read_resource_bytes(stream: BinaryIO) -> bytes:
    """Read either an SDK BytesIO response or a regular binary stream."""
    getvalue = getattr(stream, "getvalue", None)
    data = getvalue() if callable(getvalue) else stream.read()
    if not isinstance(data, bytes):
        raise ResourceValidationError("消息资源响应不是二进制内容")
    return data


def validate_downloaded_resource(
    *,
    data: bytes,
    resource_key: str,
    resource_type: ResourceType,
    file_name: str | None,
    headers: Mapping[str, object] | None = None,
) -> ValidatedResource:
    """Validate response bytes and return a safe, format-correct file name."""
    if resource_type not in {"image", "file"}:
        raise ResourceValidationError(f"非法资源类型: {resource_type}")
    if not isinstance(data, bytes) or not data:
        raise ResourceValidationError("下载资源内容为空")

    normalized_headers = {
        str(key).lower(): str(value)
        for key, value in (headers or {}).items()
        if value is not None
    }
    _validate_content_length(data, normalized_headers.get("content-length"))

    mime_type = _normalize_mime(normalized_headers.get("content-type"))
    safe_original_name = _safe_name(file_name)
    original_extension = Path(safe_original_name).suffix.lower()
    signature_format = _detect_signature(data)
    mime_format = MIME_FORMATS.get(mime_type)
    extension_format = EXTENSION_FORMATS.get(original_extension)

    detected_format = _resolve_format(
        data=data,
        resource_type=resource_type,
        signature_format=signature_format,
        mime_format=mime_format,
        extension_format=extension_format,
        mime_type=mime_type,
        original_extension=original_extension,
    )

    if resource_type == "image" and detected_format not in IMAGE_FORMATS:
        raise ResourceValidationError(
            f"资源声明为图片，但实际格式为 {detected_format or '未知'}"
        )

    _validate_mime_consistency(mime_type, mime_format, detected_format)
    extension = _choose_extension(
        detected_format,
        mime_type,
        original_extension,
    )
    base_name = Path(safe_original_name).stem if safe_original_name else ""
    if not base_name:
        base_name = _safe_stem(resource_key)
    final_name = f"{base_name}{extension}"

    return ValidatedResource(
        data=data,
        file_name=final_name,
        mime_type=mime_type or "application/octet-stream",
        detected_format=detected_format,
        extension=extension,
    )


def _resolve_format(
    *,
    data: bytes,
    resource_type: ResourceType,
    signature_format: str | None,
    mime_format: str | None,
    extension_format: str | None,
    mime_type: str,
    original_extension: str,
) -> str:
    # Binary signatures are the strongest source of truth.
    if signature_format:
        if signature_format == "ogg" and mime_format == "opus":
            return "opus"
        if signature_format == "mp4" and (
            mime_format == "m4a" or extension_format == "m4a"
        ):
            # M4A is an audio profile of the same ISO BMFF container used by
            # MP4. MIME and the original extension provide the distinction.
            return "m4a"
        return signature_format

    claimed_format = mime_format or extension_format
    if claimed_format == "json":
        try:
            json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResourceValidationError("JSON MIME/扩展名与实际内容不匹配") from exc
        return "json"

    if claimed_format == "text":
        _validate_plain_text(data)
        return "text"

    # Supported binary formats must have their expected signature. This blocks
    # renamed executables or archives from being trusted as documents/media.
    if claimed_format in STRICT_FORMATS:
        raise ResourceValidationError(
            f"{mime_type or original_extension or resource_type} 与文件签名不匹配"
        )

    # Unknown file formats may still be saved under their original extension;
    # the upper assembly layer decides whether Codex supports that extension.
    if resource_type == "file" and original_extension:
        return extension_format or original_extension.removeprefix(".")

    raise ResourceValidationError("无法从 MIME、文件名和文件签名识别资源格式")


def _detect_signature(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"BM"):
        return "bmp"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "mp4"
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        return "zip"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    if data.startswith(b"OggS"):
        return "ogg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"ID3") or (
        len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
    ):
        return "mp3"
    return None


def _validate_mime_consistency(
    mime_type: str,
    mime_format: str | None,
    detected_format: str,
) -> None:
    if mime_type in GENERIC_MIME_TYPES or mime_format is None:
        return
    compatible = (
        mime_format == detected_format
        or {mime_format, detected_format} <= {"ogg", "opus"}
        or {mime_format, detected_format} <= {"mp4", "m4a"}
    )
    if not compatible:
        raise ResourceValidationError(
            f"MIME {mime_type} 与文件签名 {detected_format} 不匹配"
        )


def _choose_extension(
    detected_format: str,
    mime_type: str,
    original_extension: str,
) -> str:
    # Preserve the extension supplied by Lark whenever it agrees with the
    # validated content. Only repair the name when metadata and bytes disagree.
    if EXTENSION_FORMATS.get(original_extension) == detected_format:
        return original_extension
    if detected_format in FORMAT_EXTENSIONS:
        return FORMAT_EXTENSIONS[detected_format]
    if original_extension:
        return original_extension
    raise ResourceValidationError(
        f"无法为 {mime_type or detected_format} 确定安全的文件扩展名"
    )


def _validate_plain_text(data: bytes) -> None:
    if b"\x00" in data:
        raise ResourceValidationError("文本文件包含二进制空字节")
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ResourceValidationError("文本文件不是有效的 UTF-8") from exc


def _validate_content_length(data: bytes, value: str | None) -> None:
    if not value:
        return
    try:
        expected = int(value)
    except ValueError as exc:
        raise ResourceValidationError("响应 Content-Length 非法") from exc
    if expected != len(data):
        raise ResourceValidationError(
            f"资源下载不完整: Content-Length={expected}, 实际={len(data)}"
        )


def _normalize_mime(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _safe_name(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^\w.()\-\u4e00-\u9fff]+", "_", Path(value).name).strip("._")


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^\w\-]+", "_", value).strip("._")
    return stem or CONFIG["resources"]["fileName"]["fallbackStem"]
