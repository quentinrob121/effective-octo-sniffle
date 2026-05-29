from __future__ import annotations

import logging
from unittest.mock import patch

from alpaca_starter.cli import setup_logging, warn_if_live


def test_setup_logging_uses_info_level_when_not_verbose():
    # pytest installs its own root logger, so check what basicConfig was
    # asked to do rather than the post-hoc level (which pytest may overwrite).
    with patch("alpaca_starter.cli.logging.basicConfig") as bc:
        setup_logging(verbose=False)
    bc.assert_called_once()
    assert bc.call_args.kwargs["level"] == logging.INFO


def test_setup_logging_uses_debug_level_when_verbose():
    with patch("alpaca_starter.cli.logging.basicConfig") as bc:
        setup_logging(verbose=True)
    bc.assert_called_once()
    assert bc.call_args.kwargs["level"] == logging.DEBUG


def test_warn_if_live_silent_for_paper(caplog):
    # Default URL in the fixture is paper; ensure no warning fires even when
    # the env var is missing entirely.
    with patch.dict("os.environ", {"ALPACA_BASE_URL": "https://paper-api.alpaca.markets/v2"}, clear=False):
        with patch("alpaca_starter.cli.load_dotenv", lambda: None):
            with caplog.at_level(logging.WARNING, logger="wheel_bot_test"):
                warn_if_live("wheel_bot_test")
    assert "LIVE" not in caplog.text


def test_warn_if_live_warns_for_non_paper(caplog):
    with patch.dict("os.environ", {"ALPACA_BASE_URL": "https://api.alpaca.markets/v2"}, clear=False):
        with patch("alpaca_starter.cli.load_dotenv", lambda: None):
            with caplog.at_level(logging.WARNING, logger="wheel_bot_test"):
                warn_if_live("wheel_bot_test")
    assert "LIVE" in caplog.text


def test_warn_if_live_silent_when_url_unset(caplog):
    with patch.dict("os.environ", {}, clear=True):
        with patch("alpaca_starter.cli.load_dotenv", lambda: None):
            with caplog.at_level(logging.WARNING, logger="wheel_bot_test"):
                warn_if_live("wheel_bot_test")
    assert caplog.text == ""
