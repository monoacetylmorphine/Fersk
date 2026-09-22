"""业务日志等级与 SDK DEBUG 独立，连接凭据不进入输出。"""

from __future__ import annotations

import importlib.util
import io
import logging
from pathlib import Path
import unittest

from fersk_codex.utils import logger as codex_logger


class BusinessLoggerTests(unittest.TestCase):
    def test_levels_traceback_redaction_and_sdk_debug(self) -> None:
        path = Path(__file__).resolve().parents[2] / "fersk_mcp/utils/logger.py"
        spec = importlib.util.spec_from_file_location("mcp_logger_test", path)
        mcp_logger = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp_logger)
        for module in (codex_logger, mcp_logger):
            business = module.get_logger("TEST")
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            previous_handlers = business.logger.handlers[:]
            previous_level = business.logger.level
            sdk = logging.Logger("test-sdk", logging.DEBUG)
            sdk.addHandler(handler)
            try:
                business.logger.handlers = [handler]
                module.configure_logging("DEBUG")
                business.debug("业务 DEBUG 可见")
                module.configure_logging("INFO")
                business.debug("不应显示")
                try:
                    raise ValueError("异常堆栈")
                except ValueError:
                    business.exception("业务失败")
                module.protect_sdk_logs(sdk)
                sdk.debug("wss://example.invalid/ws?access_key=secret&ticket=hidden&aid=1")
                text = stream.getvalue()
                self.assertIn("业务 DEBUG 可见", text)
                self.assertNotIn("不应显示", text)
                self.assertIn("Traceback", text)
                self.assertNotIn("secret", text)
                self.assertNotIn("hidden", text)
                self.assertIn("aid=1", text)
                self.assertEqual(sdk.level, logging.DEBUG)
            finally:
                business.logger.handlers = previous_handlers
                business.logger.setLevel(previous_level)
