from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

PAPER_HOST = "paper-api.alpaca.markets"


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_secret: str
    base_url: str

    @property
    def is_paper(self) -> bool:
        return PAPER_HOST in self.base_url


def load_settings() -> Settings:
    """Read Alpaca credentials from the environment (and a .env file if present)."""
    load_dotenv()

    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_API_SECRET")
    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")

    missing = [
        name
        for name, value in (
            ("ALPACA_API_KEY", api_key),
            ("ALPACA_API_SECRET", api_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill in your keys."
        )

    return Settings(api_key=api_key, api_secret=api_secret, base_url=base_url)
