#!/usr/bin/env python3
"""Render deterministic PNG variants of the original project brand mark."""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError as err:  # pragma: no cover - developer utility guard
    raise SystemExit("Pillow is required to render brand assets") from err

DARK = "#20283a"
CYAN = "#40d2e8"
ORANGE = "#ff8a3d"
SLATE = "#8d9bb6"


def _scale(value: int, size: int) -> int:
    return round(value * size / 512)


def _server_mark(image: Image.Image, box: tuple[int, int, int, int]) -> None:
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = box
    size = min(right - left, bottom - top)

    def xy(x1: int, y1: int, x2: int, y2: int) -> tuple[int, int, int, int]:
        return (
            left + _scale(x1, size),
            top + _scale(y1, size),
            left + _scale(x2, size),
            top + _scale(y2, size),
        )

    radius = _scale(38, size)
    draw.rounded_rectangle(xy(56, 72, 456, 224), radius=radius, fill=DARK)
    draw.ellipse(xy(98, 126, 142, 170), fill=ORANGE)
    draw.rounded_rectangle(xy(174, 128, 392, 168), radius=_scale(20, size), fill=CYAN)
    draw.rounded_rectangle(xy(56, 288, 456, 440), radius=radius, fill=DARK)
    draw.ellipse(xy(98, 342, 142, 386), fill=CYAN)
    draw.rounded_rectangle(xy(174, 344, 392, 384), radius=_scale(20, size), fill=SLATE)
    width = max(1, _scale(24, size))
    draw.line(
        (left + _scale(112, size), top + _scale(224, size), left + _scale(112, size), top + _scale(288, size)),
        fill=DARK,
        width=width,
    )
    draw.line(
        (left + _scale(400, size), top + _scale(224, size), left + _scale(400, size), top + _scale(288, size)),
        fill=DARK,
        width=width,
    )


def render_icon(size: int, destination: Path) -> None:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _server_mark(image, (0, 0, size, size))
    image.save(destination, format="PNG", optimize=True)


def render_logo(width: int, height: int, destination: Path) -> None:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    mark_size = height
    _server_mark(image, (round(width * 0.0125), 0, round(width * 0.0125) + mark_size, mark_size))
    scale_x = width / 960
    scale_y = height / 512

    def rect(x1: int, y1: int, x2: int, y2: int, color: str) -> None:
        draw.rounded_rectangle(
            (round(x1 * scale_x), round(y1 * scale_y), round(x2 * scale_x), round(y2 * scale_y)),
            radius=round(28 * scale_y),
            fill=color,
        )

    rect(544, 120, 888, 176, DARK)
    rect(544, 228, 808, 284, CYAN)
    rect(544, 336, 856, 392, SLATE)
    draw.ellipse(
        (round(886 * scale_x), round(120 * scale_y), round(942 * scale_x), round(176 * scale_y)),
        fill=ORANGE,
    )
    image.save(destination, format="PNG", optimize=True)


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "custom_components/nitrado_gameserver/brand"
    root.mkdir(parents=True, exist_ok=True)
    render_icon(256, root / "icon.png")
    render_icon(512, root / "icon@2x.png")
    render_logo(512, 256, root / "logo.png")
    render_logo(1024, 512, root / "logo@2x.png")
    print(f"rendered brand assets in {root}")


if __name__ == "__main__":
    sys.exit(main())
