"""Verify model routing at thread submission without model or network calls."""

from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from openai_codex import LocalImageInput, MentionInput, TextInput
from openai_codex.types import TurnStatus

from fersk_codex.core import codex
from fersk_codex.utils.config_loader import CONFIG


class ModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_and_attachment_routes_for_new_and_resumed_threads(self):
        routes = {
            key: {"model": key + "-model", "provider": key + "-provider"}
            for key in ("text", "image", "multimodal")
        }
        texts = [TextInput(text="first"), TextInput(text="second")]
        image = LocalImageInput(path="/tmp/image.png")
        document = MentionInput(name="report.pdf", path="/tmp/report.pdf")
        cases = [
            ("hello", "text"),
            ([texts[0]], "text"),
            (texts, "text"),
            ([*texts, image], "image"),
            ([*texts, document], "multimodal"),
            ([*texts, document, image], "image"),
        ]

        async def stream():
            yield NS(method="turn/completed", payload=NS(turn=NS(
                status=TurnStatus.completed, duration_ms=1)))

        for existing in (None, "thread"):
            for prompt, route in cases:
                with self.subTest(existing=existing, prompt=prompt, route=route):
                    handle = NS(id="turn", stream=stream)
                    thread = NS(id="thread", turn=AsyncMock(return_value=handle))
                    with (
                        patch.dict(CONFIG["codex"]["models"], routes),
                        patch.object(codex, "get_user_thread", AsyncMock(return_value=existing)),
                        patch.object(codex, "set_user_thread", AsyncMock()),
                        patch.object(codex, "prepare_workspace", AsyncMock()),
                        patch.object(codex, "SavingLog", AsyncMock()),
                        patch.object(codex, "AsyncCodex") as factory,
                    ):
                        client = factory.return_value.__aenter__.return_value
                        client.thread_start.return_value = thread
                        client.thread_resume.return_value = thread
                        events = [event async for event in codex.FerskCodex.running("user", prompt)]
                        submit = client.thread_resume if existing else client.thread_start
                        self.assertEqual(submit.await_args.kwargs["model"], routes[route]["model"])
                        self.assertEqual(submit.await_args.kwargs["model_provider"], routes[route]["provider"])
                        thread.turn.assert_awaited_once_with(input=prompt)
                        self.assertEqual(events, [{"type": "done"}])
