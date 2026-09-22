"""私聊历史选择卡片；表单确认、发送和回调上下文独立于普通消息卡片。"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, NotRequired, TYPE_CHECKING, TypeVar, TypedDict
from uuid import uuid4

from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
    PatchMessageRequest, PatchMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

from fersk_codex.services.lark.lark_requests import call_lark
from fersk_codex.configs.loader import CONFIG

if TYPE_CHECKING:
    from lark_oapi.core.model import BaseResponse
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT", bound="BaseResponse")


# 来源：用户要求，默认展示最近 30 条；与网关的选项总数上限一致。
PAGE_SIZE = CONFIG["messaging"].get("sessionHistoryLimit", 30)
# 来源：本功能的初始交互/内存策略，非飞书平台上限。卡片失效后重新 /history。
CARD_TTL_SECONDS = 24 * 60 * 60
MAX_CARDS = 1024
SELECT_NAME = "history_thread"


class HistoryOption(TypedDict):
    """历史选择项仍以原字典形态传递，不改变卡片协议。"""

    label: str
    value: str
    updated_at: NotRequired[int | None]


@dataclass
class HistoryCard:
    token: str
    user_id: str
    chat_id: str
    options: tuple[HistoryOption, ...]
    created_at: float
    message_id: str = ""
    page: int = 0
    revision: int = 0
    busy: bool = False
    finished: bool = False

    @property
    def visible_options(self) -> tuple[HistoryOption, ...]:
        return self.options[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]

    def message_event(self) -> SimpleNamespace:
        """仅用服务端保存的私聊上下文适配现有恢复入口。"""
        return SimpleNamespace(event=SimpleNamespace(
            message=SimpleNamespace(chat_id=self.chat_id, chat_type="p2p"),
            sender=SimpleNamespace(sender_id=SimpleNamespace(union_id=self.user_id)),
        ))


class HistoryCardStore:
    """仅主事件循环访问；限制存活时间和数量，重启后旧卡片失效。"""

    def __init__(self) -> None:
        self.cards: dict[str, HistoryCard] = {}

    def prune(self) -> None:
        now = time.monotonic()
        for token, card in list(self.cards.items()):
            if not card.busy and now - card.created_at >= CARD_TTL_SECONDS:
                self.cards.pop(token, None)

    def create(self, user_id: str, chat_id: str, options: Iterable[HistoryOption]) -> HistoryCard:
        self.prune()
        if len(self.cards) >= MAX_CARDS:
            oldest = next((key for key, card in self.cards.items() if not card.busy), None)
            if oldest is None:
                raise RuntimeError("历史卡片处理容量已满")
            self.cards.pop(oldest)
        card = HistoryCard(uuid4().hex, user_id, chat_id, tuple(options), time.monotonic())
        self.cards[card.token] = card
        return card

    def resolve(self, data: P2CardActionTrigger) -> HistoryCard:
        """卡片参数不是身份来源；同时验证操作者和原私聊消息，拒绝转发卡片。"""
        event = data.event
        action = event.action
        value = action.value if isinstance(action.value, dict) else {}
        token = value.get("ticket")
        card = self.cards.get(token) if isinstance(token, str) else None
        if card is None or time.monotonic() - card.created_at >= CARD_TTL_SECONDS:
            raise ValueError("卡片已失效，请在私聊重新发送 /history")
        operator, context = event.operator, event.context
        if (not operator or not context or not card.message_id
                or operator.union_id != card.user_id
                or context.open_chat_id != card.chat_id
                or context.open_message_id != card.message_id):
            raise ValueError("仅允许本人在原私聊中操作历史卡片")
        if card.busy or card.finished or value.get("revision") != card.revision:
            raise ValueError("该操作已处理或正在处理，请使用最新卡片")
        return card


def _plain(content: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": content}


def _shell(elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"title": _plain("历史会话"), "template": "blue"},
        "body": {"elements": elements},
    }


def status_card(content: str) -> dict[str, Any]:
    escaped = re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", html.escape(content))
    return _shell([{"tag": "markdown", "content": escaped}])


def build_history_card(card: HistoryCard) -> dict[str, Any]:
    """采用 form_action_type=submit 的 confirm：选中不回调，取消不提交表单。

    来源：用户提供的 confirm/表单字段说明；Card 2.0 form/button 官方文档。
    """
    if not card.options:
        return status_card("暂无历史会话")
    value = {"ticket": card.token, "revision": card.revision}
    elements = [
        {"tag": "markdown", "content": "选择历史会话后点击激活，确认后才会切换。"},
        {"tag": "form", "name": "history_form", "elements": [
            {"tag": "select_static", "name": SELECT_NAME, "required": True,
             "placeholder": _plain("请选择历史会话"), "width": "fill",
             "options": [{"text": _plain(item["label"]), "value": item["value"]}
                         for item in card.visible_options]},
            {"tag": "button", "name": "activate_history", "type": "primary",
             "text": _plain("激活此会话"), "form_action_type": "submit",
             "behaviors": [{"type": "callback", "value": {**value, "action": "activate_history"}}],
             "confirm": {
                 "title": _plain("确认切换历史会话？"),
                 "text": _plain("确认后将停止当前正在执行的任务，并将所选历史会话设为激活线程。后续消息将在该会话中继续。"),
             }},
        ]},
    ]
    pages = (len(card.options) + PAGE_SIZE - 1) // PAGE_SIZE
    if pages > 1:
        elements.append({"tag": "markdown", "content": f"第 {card.page + 1}/{pages} 页"})
        for label, page in (("上一页", card.page - 1), ("下一页", card.page + 1)):
            if 0 <= page < pages:
                elements.append({"tag": "button", "text": _plain(label), "behaviors": [
                    {"type": "callback", "value": {**value, "action": "history_page", "page": page}},
                ]})
    return _shell(elements)


def confirmed_thread_id(data: P2CardActionTrigger, card: HistoryCard) -> str:
    action = data.event.action
    # 不接受单纯 select_static、action.option 或未知按钮触发激活。
    if (action.tag != "button" or action.name != "activate_history"
            or not isinstance(action.value, dict)
            or action.value.get("action") != "activate_history"):
        raise ValueError("请通过激活按钮确认选择")
    values = action.form_value
    selected = values.get(SELECT_NAME) if isinstance(values, dict) else None
    if not isinstance(selected, str) or not any(item["value"] == selected for item in card.visible_options):
        raise ValueError("请选择当前卡片中的历史会话")
    return selected


def callback_response(content: str, *, error: bool = False) -> P2CardActionTriggerResponse:
    return P2CardActionTriggerResponse({"toast": {"type": "error" if error else "info", "content": content}})


async def _request(operation: Callable[[RequestT], ResponseT], request: RequestT) -> ResponseT:
    response = await call_lark(operation, request)
    if not response.success():
        raise RuntimeError(f"交互卡片请求失败: code={response.code}, log_id={response.get_log_id()}")
    return response


async def send_interactive_card(user_id: str, body: dict[str, Any]) -> str:
    # 延迟加载鉴权入口，纯卡片构建和离线回调测试无需凭据。
    from fersk_codex.services.lark.lark_client import client
    request = (CreateMessageRequest.builder().receive_id_type("union_id")
        .request_body(CreateMessageRequestBody.builder().receive_id(user_id)
            .msg_type("interactive").content(json.dumps(body, ensure_ascii=False)).build()).build())
    response = await _request(client.im.v1.message.create, request)
    return response.data.message_id


async def update_interactive_card(message_id: str, body: dict[str, Any]) -> None:
    from fersk_codex.services.lark.lark_client import client
    request = (PatchMessageRequest.builder().message_id(message_id)
        .request_body(PatchMessageRequestBody.builder()
            .content(json.dumps(body, ensure_ascii=False)).build()).build())
    await _request(client.im.v1.message.patch, request)
