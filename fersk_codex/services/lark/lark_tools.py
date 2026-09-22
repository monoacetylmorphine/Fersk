from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageReactionResponse,
    DeleteMessageReactionRequest,
    DeleteMessageReactionResponse,
    Emoji,
    GetMessageResourceRequest,
    GetMessageResourceResponse,
    ListMessageRequest,
    ListMessageResponse,
    Message,
)

from fersk_codex.configs.loader import CONFIG

from fersk_codex.services.lark.lark_client import client
from fersk_codex.services.lark.lark_requests import call_lark
from fersk_codex.utils.logger import get_logger

logger = get_logger("LarkTools")
from fersk_codex.middleware.resource_validator import (
    ResourceValidationError,
    read_resource_bytes,
    validate_downloaded_resource,
)


async def adding_reaction_emoji(message_id: str) -> str | None:

    request: CreateMessageReactionRequest = CreateMessageReactionRequest.builder() \
            .message_id(message_id) \
            .request_body(CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder()
                    .emoji_type(CONFIG["lark"]["reaction"]["processingEmoji"])
                    .build())
                .build()) \
            .build()

    response: CreateMessageReactionResponse = await call_lark(client.im.v1.message_reaction.create, request)

    if not response.success():
        _log_lark_failure(response, "client.im.v1.message_reaction.create")
        return

    lark.logger.info(lark.JSON.marshal(response.data, indent=4))
    return response.data.reaction_id


async def delete_reaction_emoji(message_id: str, reaction_id: str) -> bool:

    request: DeleteMessageReactionRequest = DeleteMessageReactionRequest.builder() \
        .message_id(message_id) \
        .reaction_id(reaction_id) \
        .build()

    response: DeleteMessageReactionResponse = await call_lark(client.im.v1.message_reaction.delete, request)

    if not response.success():
        _log_lark_failure(response, "client.im.v1.message_reaction.delete")
        return False

    lark.logger.info(lark.JSON.marshal(response.data, indent=4))
    return True


async def download_msg_resource(
    union_id: str,
    message_id: str,
    resource_key: str,
    resource_type: str,
) -> str | None:

    logger.debug("获取消息资源: message_id=%s, type=%s", message_id, resource_type)

    request: GetMessageResourceRequest = GetMessageResourceRequest.builder() \
        .message_id(message_id) \
        .file_key(resource_key) \
        .type(resource_type) \
        .build()

    response: GetMessageResourceResponse = await call_lark(client.im.v1.message_resource.get, request)

    if not response.success():
        _log_lark_failure(response, "client.im.v1.message_resource.get")
        return

    # 响应读取、校验和磁盘写入整体移出事件循环。
    return await asyncio.to_thread(_save_resource, response, union_id, message_id, resource_key, resource_type)


def _save_resource(
    response: GetMessageResourceResponse,
    union_id: str,
    message_id: str,
    resource_key: str,
    resource_type: str,
) -> str:
    saving_path = (
        Path(CONFIG["storage"]["workspaceRoot"])
        / union_id
        / CONFIG["storage"]["inboundSubdirectory"]
    )
    saving_path.mkdir(parents=True, exist_ok=True)

    try:
        data = read_resource_bytes(response.file)
        resource = validate_downloaded_resource(
            data=data,
            resource_key=resource_key,
            resource_type=resource_type,
            file_name=response.file_name,
            headers=response.raw.headers,
        )
    except ResourceValidationError as error:
        lark.logger.error(
            f"消息资源校验失败, message_id={message_id}, "
            f"resource_key={resource_key}, error={error}"
        )
        raise

    file_stem = Path(resource.file_name).stem
    file_ext = Path(resource.file_name).suffix
    timestamp = datetime.now(
        timezone(timedelta(hours=CONFIG["runtime"]["timezoneOffsetHours"]))
    ).strftime(CONFIG["resources"]["fileName"]["timestampFormat"])
    file_path = saving_path / f"{file_stem}_{timestamp}{file_ext}"
    with open(file_path, "wb") as file:
        file.write(resource.data)

    logger.debug("资源已保存: path=%s", file_path)
    return str(file_path)

async def getting_chat_history(chat_id: str, messages_num: int) -> list[Message]:

    request: ListMessageRequest = ListMessageRequest.builder() \
            .container_id_type("chat") \
            .container_id(chat_id) \
            .sort_type("ByCreateTimeDesc") \
            .page_size(messages_num) \
            .build()

    response: ListMessageResponse = await call_lark(client.im.v1.message.list, request)

    if not response.success():
        _log_lark_failure(response, "client.im.v1.message.list")
        return []

    return list(response.data.items or [])


def _log_lark_failure(
    response: CreateMessageReactionResponse | DeleteMessageReactionResponse | GetMessageResourceResponse | ListMessageResponse,
    action: str,
) -> None:
    """复用同一失败日志格式，保留各调用方原有返回契约。"""
    lark.logger.error(
        f"{action} failed, code: {response.code}, msg: {response.msg}, "
        f"log_id: {response.get_log_id()}, resp: \n"
        f"{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}"
    )
