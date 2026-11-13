"""The pure rules of the screenshot ladder: no browser."""

import base64
import struct
import zlib

import pytest

from browser_tools_plugin.screenshots import Attempt, Screenshot, fits_bytes, fits_side, image_size, ladder


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    dqt = b"\xff\xdb" + struct.pack(">H", 67) + bytes(65)
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + dqt + sof + b"\xff\xd9"


def test_image_size_reads_png_and_jpeg_headers():
    assert image_size(png(1280, 720)) == (1280, 720)
    assert image_size(jpeg(2560, 1440)) == (2560, 1440)


def test_image_size_rejects_other_data():
    with pytest.raises(ValueError):
        image_size(b"GIF89a....")


def test_the_default_ladder_is_a_viewport_jpeg_q60_then_q40_q25_q10():
    steps = list(ladder(full_page=False, type="jpeg", quality=None))
    assert steps == [Attempt(False, "jpeg", q) for q in (60, 40, 25, 10)]


def test_a_full_page_png_request_falls_back_to_viewport_jpeg_at_the_default_quality():
    steps = list(ladder(full_page=True, type="png", quality=None))
    assert steps == [Attempt(True, "png", None), *(Attempt(False, "jpeg", q) for q in (60, 40, 25, 10))]


def test_a_full_page_jpeg_request_tries_the_viewport_at_the_same_quality_first():
    steps = list(ladder(full_page=True, type="jpeg", quality=80))
    assert steps == [Attempt(True, "jpeg", 80), *(Attempt(False, "jpeg", q) for q in (80, 40, 25, 10))]


def test_a_low_requested_quality_is_never_raised_and_nothing_repeats():
    steps = list(ladder(full_page=False, type="jpeg", quality=30))
    assert steps == [Attempt(False, "jpeg", q) for q in (30, 25, 10)]
    assert list(ladder(full_page=False, type="jpeg", quality=10)) == [Attempt(False, "jpeg", 10)]
    assert list(ladder(full_page=True, type="jpeg", quality=10)) == [
        Attempt(True, "jpeg", 10),
        Attempt(False, "jpeg", 10),
    ]


def test_the_quality_is_rounded():
    assert next(iter(ladder(full_page=False, type="jpeg", quality=44.6))).quality == 45


def test_the_byte_limit_counts_base64_not_raw_bytes():
    assert fits_bytes(bytes(750_000))  # 1,000,000 base64 characters exactly
    assert not fits_bytes(bytes(750_001))


def test_the_side_limit_is_2000_px_inclusive():
    assert fits_side(2000, 100)
    assert not fits_side(100, 2001)


def test_the_label_names_what_was_sent():
    shot = Screenshot(b"x", "jpeg", 40, False, 1280, 720, scaled=False)
    assert shot.label == "Screenshot: viewport, jpeg q40, 1280x720"
    scaled = Screenshot(b"x", "png", None, True, 800, 2000, scaled=True)
    assert scaled.label == "Screenshot: full page, png, 800x2000, scaled down"
    assert base64.b64decode(shot.base64) == b"x"
