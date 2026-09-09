import json
import asyncio
from pathlib import Path

from datetime import datetime, timezone, timedelta

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

from fersk_codex.utils.config_loader import CONFIG

from fersk_codex.services.lark.lark_client import client
from fersk_codex.middleware.resource_validator import (
    ResourceValidationError,
    read_resource_bytes,
    validate_downloaded_resource,
)


async def adding_reaction_emoji(message_id:str):

    request: CreateMessageReactionRequest = CreateMessageReactionRequest.builder() \
            .message_id(message_id) \
            .request_body(CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder()
                    .emoji_type(CONFIG["lark"]["reaction"]["processingEmoji"])
                    .build())
                .build()) \
            .build()

    # 发起请求
    response: CreateMessageReactionResponse = await asyncio.to_thread(client.im.v1.message_reaction.create,request)

    # 处理失败返回
    if not response.success():
        lark.logger.error(
            f"client.im.v1.message_reaction.create failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
        return

    # 处理业务结果
    lark.logger.info(lark.JSON.marshal(response.data, indent=4))
    return response.data.reaction_id


async def delete_reaction_emoji(message_id:str, reaction_id:str) -> bool:

    request: DeleteMessageReactionRequest = DeleteMessageReactionRequest.builder() \
        .message_id(message_id) \
        .reaction_id(reaction_id) \
        .build()

    # 发起请求
    response: DeleteMessageReactionResponse = await asyncio.to_thread(client.im.v1.message_reaction.delete,request)

    # 处理失败返回
    if not response.success():
        lark.logger.error(
            f"client.im.v1.message_reaction.delete failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
        return False

    # 处理业务结果
    lark.logger.info(lark.JSON.marshal(response.data, indent=4))
    return True


async def download_msg_resource(union_id:str, message_id:str, resource_key:str, resource_type:str):

    print(f"获取消息资源: {union_id}-{message_id}-{resource_type}-{resource_key}")

    request: GetMessageResourceRequest = GetMessageResourceRequest.builder() \
        .message_id(message_id) \
        .file_key(resource_key) \
        .type(resource_type) \
        .build()

    # 发起请求
    response: GetMessageResourceResponse = await asyncio.to_thread(client.im.v1.message_resource.get,request)

    # 处理失败返回
    if not response.success():
        lark.logger.error(
            f"client.im.v1.message_resource.get failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
        return
    
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
        return

    file_stem = Path(resource.file_name).stem
    file_ext = Path(resource.file_name).suffix
    timestamp = datetime.now(
        timezone(timedelta(hours=CONFIG["runtime"]["timezoneOffsetHours"]))
    ).strftime(CONFIG["resources"]["fileName"]["timestampFormat"])
    file_path = saving_path / f"{file_stem}_{timestamp}{file_ext}"
    with open(file_path, "wb") as file:
        file.write(resource.data)

    print(file_path)
    return str(file_path)

async def getting_chat_history(chat_id:str, messages_num:int):

    request: ListMessageRequest = ListMessageRequest.builder() \
            .container_id_type("chat") \
            .container_id(chat_id) \
            .sort_type("ByCreateTimeDesc") \
            .page_size(messages_num) \
            .build()

    # 发起请求
    response: ListMessageResponse = await asyncio.to_thread(client.im.v1.message.list,request)
    
    # 处理失败返回
    if not response.success():
        lark.logger.error(
            f"client.im.v1.message.list failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
        return []

    # 处理业务结果
    # lark.logger.info(lark.JSON.marshal(response.data, indent=4))
    return list(response.data.items or [])