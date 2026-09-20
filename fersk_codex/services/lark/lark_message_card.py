"""使用参考项目的 Card 2.0 样式发送普通通知和流式回复。"""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from lark_oapi.api.cardkit.v1 import (
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
)
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from fersk_codex.services.lark.lark_client import client
from fersk_codex.services.lark.lark_requests import call_lark
from fersk_codex.utils.config_loader import CONFIG
from fersk_codex.utils.logger import get_logger
from fersk_codex.codex.thread_watchdog import settings

logger = get_logger("Card")
UPDATE_INTERVAL = 0.25
STREAM_LIFETIME = 9 * 60
ELEMENT_ID = "response_content"
MARKDOWN_IMAGE_PATTERN = re.compile(
    r"(?<!\\)!\[((?:\\.|[^\]\\\r\n])*)\]\(((?:\\.|[^)\\\r\n])*)\)"
)


class CardDeliveryError(RuntimeError):
    """消息交付失败，与上游任务失败分开处理。"""


class CardRequestError(CardDeliveryError):
    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class CardStreamStopped(Exception):
    """上游撤回或 /stop 请求：停止输出，丢弃尚未发送的缓冲文本。"""


@dataclass(frozen=True)
class CardReplace:
    """替换当前正文及缓冲内容，用于从推理切换到答案。"""

    content: str


class CardSteer:
    """A writer barrier: pause before the RPC, rotate only on confirmed acceptance."""

    def __init__(self, content):
        loop = asyncio.get_running_loop()
        self.content = content
        self.ready = loop.create_future()
        self.decision = loop.create_future()
        self.applied = loop.create_future()
        self.accepted = False

    def decide(self, accepted):
        if not self.decision.done():
            self.accepted = accepted
            self.decision.set_result(accepted)

    def release(self):
        for future in (self.ready, self.applied):
            if not future.done():
                future.set_result(False)


def _replace_markdown_images(content: str) -> str:
    """将飞书卡片不支持的 Markdown 图片降级为裸地址。"""
    return MARKDOWN_IMAGE_PATTERN.sub(r"\2", content)


class CardStreamSession:
    """One run's output controls; the sender remains the only CardKit writer.
    Controls wake a silent source without cancelling its pending read. There is
    at most one prefetched event, so a slow card cannot build an unbounded queue.
    The gateway serializes steer RPCs with its existing owner.controls lock.
    """

    def __init__(self, *, cancelled=lambda: False):
        self.cancelled = cancelled
        self.controls = asyncio.Queue()
        self.closed = False
        self.barriers = set()
        self.card_id = None
        self.message_id = None
        self.sequence = 0
        self.accumulated = ""
        self.sent = ""
        self.last_update = 0.0
        self.opened_at = 0.0
        self.card_number = 0
        self.pending_replacement = False
        self.placeholder = False

    @asynccontextmanager
    async def steering(self, content):
        barrier = CardSteer(content)
        self.barriers.add(barrier)
        try:
            if self.closed or self.cancelled():
                barrier.release()
            else:
                self.controls.put_nowait(barrier)
            await asyncio.shield(barrier.ready)
            yield barrier
        finally:
            barrier.decide(False)
            self.barriers.discard(barrier)

    def close(self):
        self.closed = True
        for barrier in self.barriers:
            barrier.decide(False)
            barrier.release()

    async def events(self, source):
        pending = control = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(source))
                if control is None:
                    control = asyncio.create_task(self.controls.get())
                await asyncio.wait({pending, control}, return_when=asyncio.FIRST_COMPLETED)
                if self.cancelled():
                    raise CardStreamStopped()
                # Prefer a submitted control over a simultaneous EOF/delta.
                if control.done():
                    barrier, control = control.result(), None
                    yield {"type": "card_control", "control": barrier}
                    continue
                ready, pending = pending, None
                try:
                    event = ready.result()
                except StopAsyncIteration:
                    return
                yield event
        finally:
            self.close()
            tasks = [task for task in (pending, control) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await source.aclose()


def _card(content: str, streaming: bool) -> dict:
    # 来源：project-reference/skills/lark-im/references/card/card-2.0-schema.md。
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "streaming_mode": streaming,
            "summary": {"content": "小脑袋已经开始转动啦，稍等哦~" if streaming else content[:100]},
            "style": {"text_size": {
                "body": {"default": "normal", "pc": "normal", "mobile": "normal"},
            }},
        },
        "header": {
            "title": {"tag": "plain_text", "content": "Codex"},
            "template": "blue",
            "icon": {"tag": "standard_icon", "token": "lark-logo_colorful"},
        },
        # "body": {
        #     "direction": "vertical",
        #     "padding": "12px 12px 20px 12px",
        #     "elements": [{
        #         "tag": "markdown", "element_id": ELEMENT_ID,
        #         "content": _replace_markdown_images(content), "text_size": "body",
        #     }],
        # },
        "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": _replace_markdown_images(content),
                        "text_align": "left",
                        "text_size": "normal",
                        "margin": "0px 0px 0px 0px",
                        "element_id": ELEMENT_ID,
                    },
                    {
                        "tag": "hr",
                        "margin": "0px 0px 0px 0px"
                    },
                    {
                        "tag": "column_set",
                        "horizontal_spacing": "12px",
                        "horizontal_align": "right",
                        "columns": [
                            {
                                "tag": "column",
                                "width": "weighted",
                                "elements": [
                                    {
                                        "tag": "markdown",
                                        "content": "<font color=\"grey-600\">内容由 AI 生成, 请仔细甄别</font>",
                                        "text_align": "center",
                                        "text_size": "notation",
                                        "margin": "4px 0px 0px 0px",
                                        "element_id": "footnote_text"
                                    }
                                ],
                                "padding": "0px 0px 0px 0px",
                                "direction": "vertical",
                                "horizontal_spacing": "8px",
                                "vertical_spacing": "8px",
                                "horizontal_align": "left",
                                "vertical_align": "top",
                                "margin": "0px 0px 0px 0px",
                                "weight": 1
                            },
                            {
                                "tag": "column",
                                "width": "auto",
                                "elements": [],
                                "padding": "0px 0px 0px 0px",
                                "direction": "vertical",
                                "horizontal_spacing": "8px",
                                "vertical_spacing": "8px",
                                "horizontal_align": "left",
                                "vertical_align": "top",
                                "margin": "0px 0px 0px 0px"
                            },
                            {
                                "tag": "column",
                                "width": "auto",
                                "elements": [],
                                "padding": "0px 0px 0px 0px",
                                "vertical_spacing": "8px",
                                "horizontal_align": "left",
                                "vertical_align": "top",
                                "margin": "0px 0px 0px 0px"
                            }
                        ],
                        "margin": "0px 0px 4px 0px"
                    }
                ]
            }
    }


async def _call(operation, request):
    try:
        async with asyncio.timeout(settings()["cardRequestTimeoutSeconds"]):
            response = await call_lark(operation, request)
    except Exception as exc:
        raise CardRequestError(f"飞书卡片请求异常: {type(exc).__name__}: {exc}") from exc
    if not response.success():
        raise CardRequestError(
            f"飞书卡片请求失败: code={response.code}, msg={response.msg}, "
            f"log_id={response.get_log_id()}", code=response.code,
        )
    return response


async def _send(union_id: str, content: dict) -> str:
    request = (CreateMessageRequest.builder()
        .receive_id_type("chat_id" if union_id.startswith("oc_") else "union_id")
        .request_body(CreateMessageRequestBody.builder()
            .receive_id(union_id).msg_type("interactive")
            .content(json.dumps(content, ensure_ascii=False)).build())
        .build())
    response = await _call(client.im.v1.message.create, request)
    return response.data.message_id


async def sending_card(
    union_id: str, content: str | AsyncIterable[str | CardReplace | CardSteer],
    *, session: CardStreamSession | None = None,
) -> str | None:
    """发送卡片并返回最后一张的 message_id；9 分钟关闭，按需续卡。
    union_id 来自消息批次，也兼容以 oc_ 开头的群 chat_id。
    空内容不发送；CardStreamStopped 结束流式状态且不刷新缓冲。
    """
    if isinstance(content, str):
        return await _send(union_id, _card(content, False)) if content else None
    if not isinstance(content, AsyncIterable):
        raise TypeError("content 必须是字符串或异步文本流")

    session = session or CardStreamSession()

    delivery_error = None
    pending = None
    no_chunk = object()

    def reset_card():
        session.card_id, session.sequence, session.accumulated, session.sent = None, 0, "", ""
        session.pending_replacement = False

    async def create_card():
        if session.cancelled():
            raise CardStreamStopped()
        session.card_number += 1
        body = _card(session.accumulated, True)
        # Start the deadline before the HTTP request, conservatively including latency.
        session.opened_at = time.monotonic()
        request = (CreateCardRequest.builder()
            .request_body(CreateCardRequestBody.builder().type("card_json")
                .data(json.dumps(body, ensure_ascii=False)).build()).build())
        response = await _call(client.cardkit.v1.card.create, request)
        session.card_id = response.data.card_id
        if session.cancelled():
            raise CardStreamStopped()
        session.message_id = await _send(union_id, {
            "type": "card", "data": {"card_id": session.card_id},
        })
        session.sent = session.accumulated
        session.pending_replacement = False
        session.last_update = time.monotonic()
        logger.info("流式卡片已发送: card_id=%s, part=%s", session.card_id, session.card_number)

    async def flush():
        if session.cancelled():
            raise CardStreamStopped()
        if session.accumulated == session.sent:
            return
        session.sequence += 1
        request = (ContentCardElementRequest.builder()
            .card_id(session.card_id).element_id(ELEMENT_ID)
            .request_body(ContentCardElementRequestBody.builder()
                .content(_replace_markdown_images(session.accumulated))
                .sequence(session.sequence).uuid(uuid4().hex).build())
            .build())
        try:
            await _call(client.cardkit.v1.card_element.content, request)
        except CardRequestError as exc:
            if exc.code != 300309:
                raise
            # The rejected write was not applied. Carry only the unsent suffix;
            # a replacement must instead carry its full new body.
            remainder = (session.accumulated[len(session.sent):]
                         if not session.pending_replacement and session.accumulated.startswith(session.sent) else session.accumulated)
            logger.warning("流式卡片已关闭，转续卡: card_id=%s", session.card_id)
            reset_card()
            session.accumulated = remainder
            if session.accumulated:
                await create_card()
            return
        session.sent = session.accumulated
        session.pending_replacement = False
        session.last_update = time.monotonic()
        logger.debug("流式卡片已更新: card_id=%s, sequence=%s, chars=%s, age=%.3f",
                     session.card_id, session.sequence, len(session.sent), session.last_update - session.opened_at)

    async def close_card():
        if session.card_id is None:
            return
        session.sequence += 1
        request = (SettingsCardRequest.builder().card_id(session.card_id)
            .request_body(SettingsCardRequestBody.builder()
                .settings(json.dumps({"config": {
                    "streaming_mode": False,
                    "summary": {"content": session.sent[:100]},
                }}, ensure_ascii=False))
                .sequence(session.sequence).uuid(uuid4().hex).build()).build())
        try:
            await _call(client.cardkit.v1.card.settings, request)
        except CardRequestError as exc:
            if exc.code != 300309:
                raise
        else:
            logger.info("流式卡片已关闭: card_id=%s, age=%.3f",
                        session.card_id, time.monotonic() - session.opened_at)
        finally:
            reset_card()

    async def delivery_failed(exc):
        nonlocal delivery_error
        delivery_error = exc
        logger.exception("卡片交付失败，继续消费任务事件，不重发结果不确定的请求")
        try:
            await close_card()
        except CardDeliveryError:
            logger.exception("关闭飞书卡片流式状态失败")

    try:
        iterator = aiter(content)
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(iterator))
            deadline = None
            if session.card_id is not None:
                deadline = session.opened_at + STREAM_LIFETIME
                if session.accumulated != session.sent:
                    deadline = min(deadline, session.last_update + UPDATE_INTERVAL)
            timeout = None if deadline is None else max(0, deadline - time.monotonic())
            # asyncio.wait does not cancel the upstream anext on a timer tick.
            await asyncio.wait({pending}, timeout=timeout)
            chunk = no_chunk
            ended = False
            if pending.done():
                ready, pending = pending, None
                try:
                    chunk = ready.result()
                except StopAsyncIteration:
                    ended = True
            if session.cancelled():
                raise CardStreamStopped()
            if isinstance(chunk, CardSteer):
                # No other CardKit request runs while this writer awaits the RPC.
                if not chunk.ready.done():
                    chunk.ready.set_result(True)
                accepted = await asyncio.shield(chunk.decision)
                rotated = False
                try:
                    if session.cancelled():
                        raise CardStreamStopped()
                    if accepted and delivery_error is None:
                        # Drop the old unsent buffer; already shown text remains.
                        await close_card()
                        session.accumulated = chunk.content
                        await create_card()
                        session.placeholder = True
                        rotated = True
                except CardDeliveryError as exc:
                    await delivery_failed(exc)
                finally:
                    if not chunk.applied.done():
                        chunk.applied.set_result(rotated and not session.cancelled())
                continue
            if delivery_error is not None:
                if ended:
                    break
                continue
            try:
                if ended:
                    if session.card_id is not None:
                        if session.placeholder:
                            session.accumulated = CONFIG["messages"]["steerCompleted"]
                            session.pending_replacement = True
                            session.placeholder = False
                        await flush()
                    break
                if session.card_id is not None and time.monotonic() >= session.opened_at + STREAM_LIFETIME:
                    old_card = session.card_id
                    await flush()
                    # flush may have recovered from 300309 by opening a fresh card.
                    if session.card_id == old_card:
                        await close_card()
                replace = isinstance(chunk, CardReplace)
                if replace:
                    chunk = chunk.content
                if chunk is not no_chunk:
                    if not isinstance(chunk, str):
                        raise TypeError("流式 content 只能产出字符串或 CardReplace")
                    if chunk:
                        if session.placeholder:
                            replace = True
                            session.placeholder = False
                        session.pending_replacement |= replace
                        session.accumulated = chunk if replace else session.accumulated + chunk
                        if session.card_id is None:
                            await create_card()
                if session.card_id is not None and (
                    replace or time.monotonic() - session.last_update >= UPDATE_INTERVAL
                ):
                    await flush()
            except CardDeliveryError as exc:
                await delivery_failed(exc)
                if ended:
                    break
    except CardStreamStopped:
        delivery_error = None
    finally:
        session.close()
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        try:
            await close_card()
        except CardDeliveryError as exc:
            delivery_error = delivery_error or exc
            logger.exception("关闭飞书卡片流式状态失败")
    if delivery_error is not None:
        raise delivery_error
    return session.message_id
