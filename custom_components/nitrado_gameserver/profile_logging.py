"""Secret-safe logging for externally supplied game-profile failures."""

from __future__ import annotations

import logging
from typing import Any


def log_profile_failure(
    logger: logging.Logger,
    phase: str,
    err: BaseException,
    *,
    profile_id: Any = "unknown",
    key: Any = None,
) -> None:
    """Log bounded metadata without exception text, args, or traceback."""

    logger.warning(
        "Profile boundary failure profile=%s phase=%s key=%s error_type=%s",
        str(profile_id)[:64],
        str(phase)[:64],
        "-" if key is None else str(key)[:64],
        err.__class__.__name__[:64],
    )
