"""Parse Lark payloads and assemble Codex turn input items."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union

from openai_codex import LocalImageInput, MentionInput, TextInput

from fersk_codex.utils.config_loader import CONFIG

from fersk_codex.middleware.audio_transcription import ASR, AudioConversionTimeout
from fersk_codex.services.lark.lark_tools import download_msg_resource
from fersk_codex.middleware.message_collector import MessageBatch


CodexInputItem = Union[TextInput, LocalImageInput, MentionInput]
CodexRunInput = Union[str, list[CodexInputItem]]

SUPPORTED_IMAGE_EXTENSIONS = frozenset(CONFIG["resources"]["acceptedExtensions"]["image"])
SUPPORTED_FILE_EXTENSIONS = frozenset(CONFIG["resources"]["acceptedExtensions"]["document"])
AUDIO_EXTENSIONS = frozenset(CONFIG["resources"]["acceptedExtensions"]["audio"])


class InputAssemblyError(Exception):
    """An input error that is safe to show directly to the Lark user."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass(frozen=True)
class AssemblyResult:
    codex_input: CodexRunInput | None
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class _TextPart:
    text: str


@dataclass(frozen=True)
class _ResourcePart:
    kind: Literal["image", "file", "audio"]
    message_id: str
    resource_key: str
    display_name: str


_InputPart = Union[_TextPart, _ResourcePart]


async def assemble_codex_input(batch: MessageBatch) -> AssemblyResult:
    """Build Codex input and report resources that had to be rejected.

    Supported resources remain in their original Lark order. If only part of a
    batch is usable, the usable parts and all text are sent to Codex. If none of
    its resources are usable, the entire batch (including text) is discarded.
    """
    if batch.unsupported_message_types:
        message_types = "、".join(sorted(batch.unsupported_message_types))
        raise InputAssemblyError(
            f"暂不支持 {message_types} 类型的消息，请改用文本、图片、富文本、文件或语音。"
        )

    parts, parse_rejections = _normalize_messages(batch)
    resource_parts = [part for part in parts if isinstance(part, _ResourcePart)]
    downloaded, download_rejections = await _download_resources(
        batch.union_id,
        resource_parts,
    )

    accepted: dict[_ResourcePart, Path] = {}
    format_rejections: list[str] = []
    for part, path in downloaded.items():
        if _is_supported(part, path):
            accepted[part] = path
        else:
            format_rejections.append(path.name or part.display_name)

    transcriptions, transcription_rejections = await _transcribe_audio(accepted)
    for part, path in list(accepted.items()):
        if part.kind == "audio" or path.suffix.lower() in AUDIO_EXTENSIONS:
            accepted.pop(part)

    rejected_names = _unique(
        [
            *parse_rejections,
            *download_rejections,
            *format_rejections,
            *transcription_rejections,
        ]
    )
    resource_count = len(resource_parts) + len(parse_rejections)

    # If a message had attachments but none can be used, its accompanying task
    # text must not be sent alone because that would change the user's request.
    if resource_count and not accepted and not transcriptions:
        names = "、".join(rejected_names) or "所发送的附件"
        return AssemblyResult(
            codex_input=None,
            notices=(
                f"以下附件处理失败：{names}。本次附件和文本任务均未提交，请处理后连同任务描述重新发送。",
            ),
        )

    items = _build_input_items(parts, accepted, transcriptions)
    if not items:
        raise InputAssemblyError(CONFIG["messages"]["emptyInput"])

    notices: tuple[str, ...] = ()
    if rejected_names:
        names = "、".join(rejected_names)
        notices = (
            f"以下附件无法输入，已跳过：{names}。请转换格式后重新发送。"
            "本次其余支持的内容将继续处理。",
        )

    if len(items) == 1 and isinstance(items[0], TextInput):
        return AssemblyResult(codex_input=items[0].text, notices=notices)
    return AssemblyResult(codex_input=items, notices=notices)


def _normalize_messages(
    batch: MessageBatch,
) -> tuple[list[_InputPart], list[str]]:
    parts: list[_InputPart] = []
    rejected: list[str] = []

    for message in sorted(batch.messages, key=lambda item: item.sequence):
        content = message.content

        if message.message_type == "text":
            _append_text(parts, content.get("text"))

        elif message.message_type == "image":
            _append_resource(
                parts,
                rejected,
                kind="image",
                message_id=message.message_id,
                resource_key=content.get("image_key"),
                display_name=f"图片 {message.message_id}",
            )

        elif message.message_type == "file":
            _append_resource(
                parts,
                rejected,
                kind="file",
                message_id=message.message_id,
                resource_key=content.get("file_key"),
                display_name=content.get("file_name") or f"文件 {message.message_id}",
            )

        elif message.message_type == "audio":
            # Lark audio payloads contain file_key and duration. Audio is sent
            # immediately (without the image/file collection window). It is
            # downloaded now and transcribed before Codex input is assembled.
            _append_resource(
                parts,
                rejected,
                kind="audio",
                message_id=message.message_id,
                resource_key=content.get("file_key"),
                display_name=content.get("file_name") or f"语音 {message.message_id}",
            )

        elif message.message_type == "post":
            _append_text(parts, content.get("title"))
            rows = content.get("content_v2") or content.get("content") or []
            for row in rows:
                if not isinstance(row, list):
                    continue
                for node in row:
                    if not isinstance(node, dict):
                        continue
                    if node.get("tag") == "text":
                        _append_text(parts, node.get("text"))
                    elif node.get("tag") == "img":
                        _append_resource(
                            parts,
                            rejected,
                            kind="image",
                            message_id=message.message_id,
                            resource_key=node.get("image_key"),
                            display_name=f"富文本图片 {message.message_id}",
                        )

    return parts, rejected


def _append_text(parts: list[_InputPart], value: object) -> None:
    if isinstance(value, str) and value.strip():
        parts.append(_TextPart(text=value.strip()))


def _append_resource(
    parts: list[_InputPart],
    rejected: list[str],
    *,
    kind: Literal["image", "file", "audio"],
    message_id: str,
    resource_key: object,
    display_name: str,
) -> None:
    if isinstance(resource_key, str) and resource_key:
        parts.append(_ResourcePart(kind, message_id, resource_key, display_name))
    else:
        rejected.append(f"{display_name}{CONFIG['messages']['missingResourceKeySuffix']}")


async def _download_resources(
    union_id: str,
    resources: list[_ResourcePart],
) -> tuple[dict[_ResourcePart, Path], list[str]]:
    if not resources:
        return {}, []

    tasks = [
        asyncio.create_task(
            download_msg_resource(
                union_id=union_id,
                message_id=part.message_id,
                resource_key=part.resource_key,
                resource_type="image" if part.kind == "image" else "file",
            )
        )
        for part in resources
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    downloaded: dict[_ResourcePart, Path] = {}
    rejected: list[str] = []
    for part, result in zip(resources, results):
        if isinstance(result, BaseException):
            print(f"下载 {part.display_name} 异常: {result}")
            rejected.append(f"{part.display_name}{CONFIG['messages']['downloadFailedSuffix']}")
        elif not result:
            rejected.append(f"{part.display_name}{CONFIG['messages']['downloadFailedSuffix']}")
        else:
            downloaded[part] = Path(result).resolve()

    return downloaded, rejected


def _is_supported(part: _ResourcePart, path: Path) -> bool:
    extension = path.suffix.lower()
    if part.kind == "image":
        return extension in SUPPORTED_IMAGE_EXTENSIONS
    if part.kind == "file":
        return extension in SUPPORTED_FILE_EXTENSIONS or extension in AUDIO_EXTENSIONS
    # Lark voice messages are downloaded as OGG and normalized by the audio
    # middleware regardless of the generated file name.
    return True


async def _transcribe_audio(
    accepted: dict[_ResourcePart, Path],
) -> tuple[dict[_ResourcePart, str], list[str]]:
    audio_parts = [
        (part, path)
        for part, path in accepted.items()
        if part.kind == "audio" or path.suffix.lower() in AUDIO_EXTENSIONS
    ]
    if not audio_parts:
        return {}, []

    tasks = [asyncio.create_task(ASR().transfer(path)) for _, path in audio_parts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    transcriptions: dict[_ResourcePart, str] = {}
    rejected: list[str] = []
    for (part, _), result in zip(audio_parts, results):
        if isinstance(result, AudioConversionTimeout):
            rejected.append(f"{part.display_name}：{result}")
        elif isinstance(result, BaseException):
            print(f"转写 {part.display_name} 异常: {result}")
            rejected.append(f"{part.display_name}{CONFIG['messages']['transcriptionFailedSuffix']}")
        elif not result.strip():
            rejected.append(f"{part.display_name}{CONFIG['messages']['emptyTranscriptionSuffix']}")
        else:
            transcriptions[part] = result.strip()
    return transcriptions, rejected


def _build_input_items(
    parts: list[_InputPart],
    accepted: dict[_ResourcePart, Path],
    transcriptions: dict[_ResourcePart, str],
) -> list[CodexInputItem]:
    items: list[CodexInputItem] = []
    for part in parts:
        if isinstance(part, _TextPart):
            items.append(TextInput(text=part.text))
        elif part in transcriptions:
            items.append(TextInput(text=transcriptions[part]))
        elif part in accepted:
            path = accepted[part]
            if part.kind == "image":
                items.append(LocalImageInput(path=str(path)))
            else:
                items.append(MentionInput(name=path.name, path=str(path)))
    return items


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
