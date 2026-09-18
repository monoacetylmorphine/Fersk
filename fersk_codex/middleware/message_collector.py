"""Convert descending Lark chat history into a Codex message batch."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fersk_codex.utils.config_loader import CONFIG


SUPPORTED_MESSAGE_TYPES = set(CONFIG["messaging"]["supportedTypes"])


@dataclass(frozen=True)
class CollectedMessage:
    message_id: str
    message_type: str
    content: dict[str, Any]
    sequence: int
    create_time: str | None = None


@dataclass(frozen=True)
class MessageBatch:
    chat_id: str
    union_id: str
    chat_type: str
    messages: tuple[CollectedMessage, ...]

    @property
    def unsupported_message_types(self) -> set[str]:
        return {
            message.message_type
            for message in self.messages
            if message.message_type not in SUPPORTED_MESSAGE_TYPES
        }


def batch_from_chat_history(data: Any, history_items: list[Any]) -> MessageBatch:
    """Build a chronological user-only batch from descending Lark history.

    Lark returns the newest message first.  Only messages newer than the most
    recent app reply belong to the current user turn, so scanning stops at the
    first app message.  The retained messages are then reversed for Codex.
    """
    event_message = data.event.message
    target_id = (
        event_message.chat_id
        if event_message.chat_type == "group"
        else data.event.sender.sender_id.union_id
    )
    retained_desc: list[Any] = []
    history_message_ids = {
        _field(item, "message_id")
        for item in history_items
    }
    current_id = event_message.message_id
    event_item = _history_item_from_event(data)

    if (is_stop_command(event_message.message_type, event_message.content)
            or is_new_command(event_message.message_type, event_message.content)
            or (event_message.chat_type == "p2p"
                and is_history_command(event_message.message_type, event_message.content))):
        return MessageBatch(event_message.chat_id, target_id, event_message.chat_type, ())
    else:
        # A history request can already include a later receive event. Anchor
        # this batch to its triggering message, so later input is routed only
        # after this submission has established its turn/status.
        anchor = next((index for index, item in enumerate(history_items)
                       if _field(item, "message_id") == current_id), None)
        candidates = history_items[anchor:] if anchor is not None else history_items
        current_time = getattr(event_message, "create_time", None)
        for item in candidates:
            item_time = _field(item, "create_time")
            if (anchor is None and current_time and item_time
                    and str(item_time).isdigit() and str(current_time).isdigit()
                    and int(item_time) > int(current_time)):
                continue
            sender = _field(item, "sender")
            sender_type = _field(sender, "sender_type")
            if sender_type == "app":
                break
            if sender_type == "user" and not _field(item, "deleted", False):
                # /new 和 /stop 都是历史边界，不与后续输入合并。
                if _is_new_thread_command(item) or _is_command(item, "stopThreadCommand"):
                    break
                if (event_message.chat_type == "p2p" and is_history_command(
                        _field(item, "msg_type"), _field(_field(item, "body"), "content", ""))):
                    break
                if _field(item, "msg_type") in SUPPORTED_MESSAGE_TYPES:
                    retained_desc.append(item)

    # The list API can be briefly behind the receive event. Keep the triggering
    # message in the turn even when it has not appeared in history yet.
    if (event_message.message_type in SUPPORTED_MESSAGE_TYPES
            and current_id not in history_message_ids and event_item not in retained_desc):
        retained_desc.insert(0, event_item)

    messages = tuple(
        _parse_history_message(item, sequence)
        for sequence, item in enumerate(reversed(retained_desc), start=1)
    )
    return MessageBatch(
        chat_id=event_message.chat_id,
        union_id=target_id,
        chat_type=event_message.chat_type,
        messages=messages,
    )


def _parse_history_message(item: Any, sequence: int) -> CollectedMessage:
    body = _field(item, "body")
    raw_content = _field(body, "content", "")
    try:
        content = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        content = {"raw": raw_content}

    return CollectedMessage(
        message_id=_field(item, "message_id", ""),
        message_type=_field(item, "msg_type", ""),
        content=content,
        sequence=sequence,
        create_time=_field(item, "create_time"),
    )


def _is_new_thread_command(item: Any) -> bool:
    return _is_command(item, "newThreadCommand")


def is_new_command(message_type: str, raw_content: str) -> bool:
    return _is_command({"msg_type": message_type, "body": {"content": raw_content}}, "newThreadCommand")


def is_stop_command(message_type: str, raw_content: str) -> bool:
    """与 /new 一致：仅识别独立文本，忽略首尾空白和大小写。"""
    return _is_command({"msg_type": message_type, "body": {"content": raw_content}}, "stopThreadCommand")


def is_history_command(message_type: str, raw_content: str) -> bool:
    """私聊历史入口使用固定 /history；调用方决定聊天范围，不修改配置格式。"""
    if message_type != "text":
        return False
    try:
        content = json.loads(raw_content)
    except (TypeError, json.JSONDecodeError):
        return False
    text = content.get("text") if isinstance(content, dict) else None
    return isinstance(text, str) and text.strip().lower() == "/history"


def _is_command(item: Any, config_key: str) -> bool:
    if _field(item, "msg_type") != "text":
        return False
    body = _field(item, "body")
    raw_content = _field(body, "content", "")
    try:
        content = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError):
        return False
    text = content.get("text") if isinstance(content, dict) else None
    return (
        isinstance(text, str)
        and text.strip().lower() == CONFIG["messaging"][config_key].lower()
    )


def _history_item_from_event(data: Any) -> dict[str, Any]:
    message = data.event.message
    return {
        "message_id": message.message_id,
        "msg_type": message.message_type,
        "create_time": getattr(message, "create_time", None),
        "deleted": False,
        "sender": {"sender_type": "user"},
        "body": {"content": message.content},
    }


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)
