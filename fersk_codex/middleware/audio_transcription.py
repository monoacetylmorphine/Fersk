"""Normalize downloaded audio and transcribe it before it reaches Codex."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path



from openai import AsyncOpenAI
from fersk_codex.configs.loader import CONFIG
from fersk_codex.utils.logger import get_logger

logger = get_logger("Audio")


AUDIO_CONFIG = CONFIG["audio"]
MAX_AUDIO_BYTES = AUDIO_CONFIG["limits"]["maxBytes"]
MAX_AUDIO_DURATION_SECONDS = AUDIO_CONFIG["limits"]["maxDurationSeconds"]
TARGET_AUDIO_BYTES = AUDIO_CONFIG["limits"]["targetBytes"]
TARGET_AUDIO_BITRATE = AUDIO_CONFIG["output"]["bitrateBps"]


class AudioProcessingError(RuntimeError):
    """Raised when an audio resource cannot be converted or transcribed."""


class AudioConversionTimeout(AudioProcessingError):
    """The complete conversion budget expired."""


class ASR:
    @staticmethod
    def _encode_audio(file_path: Path) -> str:
        with file_path.open("rb") as read_file:
            return base64.b64encode(read_file.read()).decode("utf-8")

    @staticmethod
    async def _reap(process):
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.communicate(), timeout=2)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()

    @staticmethod
    async def _run(command: list[str], action: str) -> subprocess.CompletedProcess[str]:
        # Shield creation so cancellation cannot orphan a late-created child.
        creating = asyncio.create_task(asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        ))
        process = None
        try:
            process = await asyncio.shield(creating)
            stdout, stderr = await process.communicate()
        except BaseException:
            async def cleanup():
                child = process
                if child is None:
                    try:
                        child = await creating
                    except Exception:
                        return
                await ASR._reap(child)
            cleaning = asyncio.create_task(cleanup())
            # Cleanup owns the process even if another stop arrives meanwhile.
            while not cleaning.done():
                try:
                    await asyncio.shield(cleaning)
                except asyncio.CancelledError:
                    continue
            cleaning.result()
            raise
        result = subprocess.CompletedProcess(command, process.returncode,
                                             stdout.decode(errors="replace"), stderr.decode(errors="replace"))
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1:] or ["未知错误"]
            raise AudioProcessingError(f"{action}失败: {detail[0]}")
        return result

    @classmethod
    async def _probe_duration(cls, file_path: Path) -> float:
        result = await cls._run(
            [
                AUDIO_CONFIG["tools"]["ffprobe"], "-v", "error", "-show_entries", "format=duration",
                "-of", "json", str(file_path),
            ],
            "读取音频时长",
        )
        try:
            duration = float(json.loads(result.stdout)["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AudioProcessingError("无法读取音频时长") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise AudioProcessingError("音频时长无效")
        return duration

    @classmethod
    async def _convert_audio(
        cls,
        input_path: Path,
        output_path: Path,
        *,
        start: float | None = None,
        duration: float | None = None,
    ) -> None:
        """Convert all input formats to 16 kHz mono, 64 kbps AAC/M4A."""
        command = [AUDIO_CONFIG["tools"]["ffmpeg"], "-v", "error", "-y"]
        if start is not None:
            command.extend(["-ss", str(start)])
        command.extend(["-i", str(input_path)])
        if duration is not None:
            command.extend(["-t", str(duration)])
        command.extend([
            "-vn", "-ac", str(AUDIO_CONFIG["output"]["channels"]),
            "-ar", str(AUDIO_CONFIG["output"]["sampleRateHz"]),
            "-c:a", AUDIO_CONFIG["output"]["codec"],
            "-b:a", str(AUDIO_CONFIG["output"]["bitrateBps"]),
        ])
        if AUDIO_CONFIG["output"]["fastStart"]:
            command.extend(["-movflags", "+faststart"])
        command.append(str(output_path))
        await cls._run(command, "ffmpeg 音频转换")

    @classmethod
    async def _prepare_segments(cls, input_path: Path, directory: Path) -> list[Path]:
        """Create API-safe M4A pieces, bounded by both bytes and duration."""
        duration = await cls._probe_duration(input_path)
        container = AUDIO_CONFIG["output"]["container"]
        normalized = directory / f"normalized.{container}"
        estimated_bytes = duration * TARGET_AUDIO_BITRATE / 8
        if (
            duration <= MAX_AUDIO_DURATION_SECONDS
            and estimated_bytes <= TARGET_AUDIO_BYTES
        ):
            await cls._convert_audio(input_path, normalized)
            if normalized.stat().st_size <= MAX_AUDIO_BYTES:
                return [normalized]
            normalized.unlink(missing_ok=True)

        bytes_based_seconds = TARGET_AUDIO_BYTES * 8 / TARGET_AUDIO_BITRATE
        segment_seconds = min(MAX_AUDIO_DURATION_SECONDS, bytes_based_seconds)
        segments: list[Path] = []
        start = 0.0
        index = 1
        while start < duration:
            piece_duration = min(segment_seconds, duration - start)
            segment = directory / f"segment-{index:04d}.{container}"
            await cls._convert_audio(
                input_path,
                segment,
                start=start,
                duration=piece_duration,
            )
            if segment.stat().st_size > MAX_AUDIO_BYTES:
                segment.unlink(missing_ok=True)
                segment_seconds /= 2
                if segment_seconds < AUDIO_CONFIG["limits"]["minimumSegmentSeconds"]:
                    raise AudioProcessingError("无法将音频切分到 25 MB 以内")
                continue
            segments.append(segment)
            start += piece_duration
            index += 1
        return segments

    @staticmethod
    def _extract_text(response: object) -> str:
        for item in getattr(response, "output", []):
            if getattr(item, "type", None) != "message":
                continue
            for content in getattr(item, "content", []):
                if getattr(content, "type", None) == "output_text":
                    return str(content.text).strip()
        raise AudioProcessingError("接口响应中没有找到识别文本")

    async def _transcribe_segment(self, client: AsyncOpenAI, path: Path) -> str:
        base64_file = await asyncio.to_thread(self._encode_audio, path)
        response = await client.responses.create(
            model=AUDIO_CONFIG["asr"]["model"],
            instructions=AUDIO_CONFIG["asr"]["instructions"],
            input=[{
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "audio_url": (
                            f"data:audio/{AUDIO_CONFIG['output']['container']};base64,"
                            + base64_file
                        ),
                    },
                    {"type": "input_text", "text": AUDIO_CONFIG["asr"]["userPrompt"]},
                ],
            }],
        )
        return self._extract_text(response)

    async def transfer(self, file_path: str | Path) -> str:
        """Convert, split when necessary, and return ordered transcription."""
        input_path = Path(file_path).expanduser().resolve()
        if not input_path.is_file():
            raise FileNotFoundError(f"音频文件不存在: {input_path}")

        temporary_directory: Path | None = None
        try:
            api_key_env = AUDIO_CONFIG["asr"]["apiKeyEnv"]
            api_key = os.getenv(api_key_env)
            if not api_key:
                raise AudioProcessingError(f"未配置 {api_key_env}")

            temporary_directory = Path(tempfile.mkdtemp(prefix=AUDIO_CONFIG["asr"]["temporaryDirectoryPrefix"]))
            try:
                async with asyncio.timeout(AUDIO_CONFIG["limits"]["conversionTimeoutSeconds"]):
                    segments = await self._prepare_segments(input_path, temporary_directory)
            except TimeoutError as exc:
                minutes = AUDIO_CONFIG["limits"]["conversionTimeoutSeconds"] / 60
                raise AudioConversionTimeout(
                    CONFIG["messages"]["audioConversionTimeout"].format(minutes=f"{minutes:g}")
                ) from exc
            async with AsyncOpenAI(
                api_key=api_key,
                base_url=AUDIO_CONFIG["asr"]["baseUrl"],
            ) as client:
                logger.debug("音频转写开始")
                texts = []
                for segment in segments:
                    text = await self._transcribe_segment(client, segment)
                    if text:
                        texts.append(text)
            logger.debug("音频转写结束")
            return "\n".join(texts)
        finally:
            if temporary_directory is not None:
                shutil.rmtree(temporary_directory, ignore_errors=True)
            # 按原文件扩展名清理，成功、失败或取消均删除 OGG，其他格式保留。
            if input_path.suffix.lower() == ".ogg":
                try:
                    input_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("删除 OGG 原文件失败: path=%s", input_path, exc_info=True)
