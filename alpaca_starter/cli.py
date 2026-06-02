from __future__ import annotations

import logging
import os

from dotenv import load_dotenv


def setup_logging(verbose: bool) -> None:
    """Configure root logging in the standard format used across modules.

    Mirrors the ``_setup_logging`` helper that each CLI used to define
    privately. Extracted so new modules don't keep copy-pasting it.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def warn_if_live(logger_name: str) -> None:
    """Emit a WARNING if ``ALPACA_BASE_URL`` is not the paper endpoint.

    Loads ``.env`` first because the URL is usually configured there; the
    underlying ``alpaca-py`` client routes by the ``paper`` boolean derived
    from this URL in ``alpaca_starter.client.build_trading_client`` — so
    this string is the authoritative signal of paper vs. live.
    """
    load_dotenv()
    base_url = os.getenv("ALPACA_BASE_URL")
    if base_url and "paper-api" not in base_url:
        logging.getLogger(logger_name).warning(
            "ALPACA_BASE_URL=%s is not the paper endpoint -- orders will be LIVE",
            base_url,
        )
