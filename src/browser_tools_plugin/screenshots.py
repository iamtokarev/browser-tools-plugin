"""The screenshot fallback ladder: pure rules, with no browser in them.

A screenshot must fit what clients accept: at most 1 MB of base64 and at most
2,000 px on the longest side. The ladder lists what to try, in order.
"""

import base64
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

MAX_BASE64_BYTES = 1_000_000
MAX_SIDE_PX = 2_000
DEFAULT_QUALITY = 60
# Viewport JPEG qualities to fall back to, after the requested options.
FALLBACK_QUALITIES = (40, 25, 10)

ImageType = Literal["png", "jpeg"]


@dataclass(frozen=True)
class Attempt:
    """One capture to try. `quality` is None for a PNG."""

    full_page: bool
    type: ImageType
    quality: int | None


@dataclass(frozen=True)
class Screenshot:
    """An image that fits the limits, and what it is."""

    data: bytes
    type: ImageType
    quality: int | None
    full_page: bool
    width: int
    height: int
    scaled: bool  # captured smaller than the page's pixel size, to keep to the side limit

    @property
    def mime_type(self) -> str:
        return f"image/{self.type}"

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def label(self) -> str:
        """The text that names what was sent, for example `Screenshot: viewport, jpeg q40, 1280x720`."""
        kind = f"{self.type} q{self.quality}" if self.quality is not None else self.type
        scope = "full page" if self.full_page else "viewport"
        text = f"Screenshot: {scope}, {kind}, {self.width}x{self.height}"
        return f"{text}, scaled down" if self.scaled else text


class ScreenshotTooLargeError(Exception):
    """No rung of the ladder gave an image that fits."""

    def __init__(self) -> None:
        super().__init__(
            f"The screenshot is larger than {MAX_BASE64_BYTES // 1_000_000} MB of base64 "
            f"or {MAX_SIDE_PX} px on a side, even as a small viewport JPEG. "
            "Make the browser window smaller, for example with "
            "`await page.setViewportSize({ width: 1280, height: 720 })` in run code, then call screenshot again."
        )


def ladder(*, full_page: bool, type: ImageType, quality: float | None) -> Iterator[Attempt]:
    """The requested options first, then a viewport JPEG at the requested quality and at lower ones.

    A PNG counts as quality 60 here. No attempt repeats.
    """
    requested_quality = None if type == "png" else round(DEFAULT_QUALITY if quality is None else quality)
    seen: set[Attempt] = set()
    first = Attempt(full_page, type, requested_quality)
    seen.add(first)
    yield first
    ceiling = DEFAULT_QUALITY if requested_quality is None else requested_quality
    for fallback in sorted({ceiling, *FALLBACK_QUALITIES}, reverse=True):
        attempt = Attempt(False, "jpeg", fallback)
        if fallback <= ceiling and attempt not in seen:
            seen.add(attempt)
            yield attempt


def fits_bytes(data: bytes) -> bool:
    return 4 * ((len(data) + 2) // 3) <= MAX_BASE64_BYTES


def image_size(data: bytes) -> tuple[int, int]:
    """The width and height of a PNG or JPEG. Raises ValueError for anything else."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    if data[:2] == b"\xff\xd8":
        position = 2
        while position + 9 < len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            marker = data[position + 1]
            if marker == 0xFF:
                position += 1
                continue
            if marker in (0x01, *range(0xD0, 0xDA)):  # markers without a length
                position += 2
                continue
            (length,) = struct.unpack(">H", data[position + 2 : position + 4])
            # SOF0 to SOF15, except DHT (C4), JPG (C8) and DAC (CC): these hold the frame size.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[position + 5 : position + 9])
                return width, height
            position += 2 + length
    raise ValueError("not a PNG or JPEG image")


def fits_side(width: int, height: int) -> bool:
    return max(width, height) <= MAX_SIDE_PX
