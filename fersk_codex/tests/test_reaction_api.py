"""使用真实 SDK 请求模型验证 reaction 删除结果，不读取凭据或访问飞书。"""

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from lark_oapi.api.im.v1 import DeleteMessageReactionRequest
from fersk_codex.services.lark.lark_requests import call_lark


class ReactionApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_reports_api_success_and_failure(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / "services/lark/lark_tools.py").read_text())
        function = next(node for node in source.body
                        if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "delete_reaction_emoji")
        for succeeded in (True, False):
            with self.subTest(succeeded=succeeded):
                response = NS(success=lambda: succeeded, data=None,
                              code=999, msg="denied", get_log_id=lambda: "log-1",
                              raw=NS(content=b"{}"))
                delete = Mock(return_value=response)
                namespace = dict(
                    asyncio=asyncio, call_lark=call_lark, json=json, PAYLOAD_INDENT=4,
                    DeleteMessageReactionRequest=DeleteMessageReactionRequest,
                    lark=NS(logger=Mock(), JSON=NS(marshal=Mock(return_value="{}"))),
                    client=NS(im=NS(v1=NS(message_reaction=NS(delete=delete)))),
                )
                exec(compile(ast.Module(body=[function], type_ignores=[]),
                             "services/lark/lark_tools.py", "exec"), namespace)
                result = await namespace["delete_reaction_emoji"]("m1", "r1")
                self.assertIs(result, succeeded)
                request = delete.call_args.args[0]
                self.assertEqual(request.message_id, "m1")
                self.assertEqual(request.reaction_id, "r1")


if __name__ == "__main__":
    unittest.main()
