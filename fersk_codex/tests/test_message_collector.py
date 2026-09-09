"""History ordering, eventual consistency and control-message boundaries."""

import copy
import json
from types import SimpleNamespace as NS
import unittest

from fersk_codex.middleware.message_collector import (
    CollectedMessage, MessageBatch, batch_from_chat_history, is_new_command, is_stop_command,
)


def event(message_id="current", chat_type="p2p", create_time="20"):
    return NS(event=NS(message=NS(message_id=message_id, chat_id="chat", chat_type=chat_type,
        message_type="text", content='{"text":"current"}', create_time=create_time),
        sender=NS(sender_id=NS(union_id="user"))))


def history(message_id, sender="user", **kwargs):
    return dict(message_id=message_id, sender={"sender_type": sender},
                msg_type="text", body={"content": json.dumps({"text": message_id})}, **kwargs)


class MessageCollectorTests(unittest.TestCase):
    def test_history_is_chronological_and_stops_at_app_reply(self):
        items = [history("current"), history("older"), history("reply", "app"), history("stale")]
        original = copy.deepcopy(items)
        result = batch_from_chat_history(event(), items)
        self.assertEqual([m.message_id for m in result.messages], ["older", "current"])
        self.assertEqual([m.sequence for m in result.messages], [1, 2])
        self.assertEqual(items, original)

    def test_deleted_unknown_sender_and_unsupported_messages_are_skipped(self):
        unsupported = history("unsupported"); unsupported["msg_type"] = "sticker"
        items = [history("current"), history("deleted", deleted=True), unsupported,
                 history("system", "system"), history("kept")]
        result = batch_from_chat_history(event(), items)
        self.assertEqual([m.message_id for m in result.messages], ["kept", "current"])

    def test_missing_event_is_inserted_once_after_older_history(self):
        result = batch_from_chat_history(event(), [history("older")])
        self.assertEqual([m.message_id for m in result.messages], ["older", "current"])
        self.assertEqual(result.messages[-1].create_time, "20")

    def test_event_anchor_excludes_later_messages_and_app_replies(self):
        result = batch_from_chat_history(event(), [history("later", "app"), history("current"), history("older")])
        self.assertEqual([m.message_id for m in result.messages], ["older", "current"])

    def test_missing_anchor_uses_numeric_timestamp_not_lexical_order(self):
        result = batch_from_chat_history(event(), [history("future", create_time="100"),
            history("equal", create_time="20"), history("older", create_time="9")])
        self.assertEqual([m.message_id for m in result.messages], ["older", "equal", "current"])

    def test_nonnumeric_timestamp_does_not_crash_or_drop_history(self):
        result = batch_from_chat_history(event(), [history("older", create_time="unknown")])
        self.assertEqual([m.message_id for m in result.messages], ["older", "current"])

    def test_sdk_objects_and_dict_history_have_same_result(self):
        item = history("current", create_time="20")
        sdk = NS(**dict(item, sender=NS(**item["sender"]), body=NS(**item["body"])))
        self.assertEqual(batch_from_chat_history(event(), [item]), batch_from_chat_history(event(), [sdk]))

    def test_group_recipient_is_chat_and_direct_recipient_is_user(self):
        for kind, target in (("p2p", "user"), ("group", "chat")):
            with self.subTest(kind=kind):
                result = batch_from_chat_history(event(chat_type=kind), [])
                self.assertEqual((result.chat_id, result.union_id, result.chat_type), ("chat", target, kind))

    def test_bad_json_is_retained_as_raw_content(self):
        for raw in ("{broken", None):
            item = history("current"); item["body"]["content"] = raw
            with self.subTest(raw=raw):
                self.assertEqual(batch_from_chat_history(event(), [item]).messages[0].content, {"raw": raw})

    def test_deleted_control_message_does_not_cut_off_history(self):
        command = history("deleted", deleted=True); command["body"]["content"] = '{"text":"/stop"}'
        result = batch_from_chat_history(event(), [history("current"), command, history("older")])
        self.assertEqual([m.message_id for m in result.messages], ["older", "current"])

    def test_commands_reject_nonobject_nontext_and_malformed_payloads(self):
        for command in (is_new_command, is_stop_command):
            for raw in (None, "[]", "null", '"/new"', "1", "true", "{", '{"text":null}'):
                with self.subTest(command=command.__name__, raw=raw):
                    self.assertFalse(command("text", raw))

    def test_batch_properties_include_unique_unsupported_types(self):
        empty = MessageBatch("c", "u", "p2p", ())
        self.assertEqual(empty.unsupported_message_types, set())
        batch = MessageBatch("c", "u", "p2p", tuple(
            CollectedMessage(str(i), kind, {}, i) for i, kind in enumerate(("text", "sticker", "sticker", "video"))))
        self.assertEqual(batch.unsupported_message_types, {"sticker", "video"})
