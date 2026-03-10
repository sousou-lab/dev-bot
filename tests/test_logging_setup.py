from __future__ import annotations

import json
import logging
import os
import unittest
from unittest.mock import patch

from app.logging_setup import configure_logging, get_logger


class LoggingSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reset root logger between tests
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)

    def test_configure_logging_sets_level_from_env(self) -> None:
        with patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}):
            configure_logging()
        root = logging.getLogger()
        self.assertEqual(root.level, logging.DEBUG)

    def test_configure_logging_defaults_to_info(self) -> None:
        with patch.dict(os.environ, {"LOG_LEVEL": ""}, clear=False):
            env = os.environ.copy()
            env.pop("LOG_LEVEL", None)
            with patch.dict(os.environ, env, clear=True):
                configure_logging()
        root = logging.getLogger()
        self.assertEqual(root.level, logging.INFO)

    def test_get_logger_returns_named_logger(self) -> None:
        logger = get_logger("test_module")
        self.assertEqual(logger.name, "test_module")
        self.assertIsInstance(logger, logging.Logger)

    def test_json_format_outputs_valid_json(self) -> None:
        with patch.dict(os.environ, {"LOG_FORMAT": "json", "LOG_LEVEL": "INFO"}):
            configure_logging()
        get_logger("test_json")
        # Capture output
        root = logging.getLogger()
        handler = root.handlers[0]
        record = logging.LogRecord(
            name="test_json",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        output = handler.formatter.format(record)
        parsed = json.loads(output)
        self.assertEqual(parsed["message"], "hello world")
        self.assertEqual(parsed["level"], "INFO")
        self.assertIn("timestamp", parsed)
        self.assertEqual(parsed["logger"], "test_json")

    def test_text_format_is_default(self) -> None:
        with patch.dict(os.environ, {"LOG_LEVEL": "INFO"}, clear=False):
            env = os.environ.copy()
            env.pop("LOG_FORMAT", None)
            with patch.dict(os.environ, env, clear=True):
                configure_logging()
        root = logging.getLogger()
        handler = root.handlers[0]
        # Text formatter should not produce JSON
        record = logging.LogRecord(
            name="test_text",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello",
            args=(),
            exc_info=None,
        )
        output = handler.formatter.format(record)
        self.assertIn("hello", output)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(output)

    def test_secret_masking_in_log_output(self) -> None:
        with patch.dict(os.environ, {"LOG_FORMAT": "json", "LOG_LEVEL": "DEBUG"}):
            configure_logging()
        get_logger("test_mask")
        root = logging.getLogger()
        handler = root.handlers[0]
        record = logging.LogRecord(
            name="test_mask",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="token=ghp_abc123secret456abcdef789012345 and key=ghs_xyz789abcdef012345678901234567",
            args=(),
            exc_info=None,
        )
        output = handler.formatter.format(record)
        self.assertNotIn("ghp_abc123secret456abcdef789012345", output)
        self.assertNotIn("ghs_xyz789abcdef012345678901234567", output)
        self.assertIn("***", output)
