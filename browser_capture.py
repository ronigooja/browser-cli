"""Bounded PNG transfer and viewport-relative cropping."""

from base64 import b64decode, b64encode
from math import ceil, floor
from pathlib import Path
import re
import struct
import subprocess
from tempfile import TemporaryDirectory


PREFIX = "data:image/png;base64,"
MAX_IMAGE = 12 * 1024 * 1024


class CaptureError(ValueError):
    pass


def collect(reply, request, clock):
    if "image" in reply:
        image = reply["image"]
        if not isinstance(image, str) or "image_ref" in reply or "image_length" in reply:
            raise CaptureError("read_unavailable")
    else:
        ref, length = reply.get("image_ref"), reply.get("image_length")
        if (not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", ref)
                or type(length) is not int or not len(PREFIX) < length <= MAX_IMAGE):
            raise CaptureError("read_unavailable")
        parts, offset = [], 0
        deadline = clock() + 60
        while offset < length:
            remaining = deadline - clock()
            if remaining <= 0 or len(parts) >= 800:
                raise CaptureError("capture_expired")
            chunk = request("capture_chunk", {"image_ref": ref, "offset": offset}, min(5, remaining))
            if chunk["status"] != "ok":
                raise CaptureError("capture_expired")
            data, next_offset = chunk.get("data"), chunk.get("next_offset")
            if not isinstance(data, str) or not 0 < len(data) <= 48000 or not data.isascii():
                raise CaptureError("read_unavailable")
            end = offset + len(data)
            if (end > length or (end < length and len(data) < 16384)
                    or not ((type(next_offset) is int and next_offset == end)
                            or (next_offset is None and end == length))):
                raise CaptureError("read_unavailable")
            parts.append(data)
            offset = end
        image = "".join(parts)
    if len(image) > MAX_IMAGE or not image.startswith(PREFIX):
        raise CaptureError("read_unavailable")
    try:
        png = b64decode(image[len(PREFIX):], validate=True)
    except ValueError:
        raise CaptureError("read_unavailable") from None
    png_size(png)
    return png


def png_size(png):
    if (len(png) < 45 or png[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
            or not png.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")):
        raise CaptureError("read_unavailable")
    w, h = struct.unpack_from(">II", png, 16)
    if not 0 < w * h <= 40_000_000 or max(w, h) > 16384:
        raise CaptureError("response_too_large")
    return w, h


def pixel_box(size, geometry):
    keys = ("x", "y", "width", "height", "viewport_width", "viewport_height")
    if not isinstance(geometry, dict) or any(type(geometry.get(k)) not in (int, float)
            or not 0 <= geometry[k] <= 65536 for k in keys):
        raise CaptureError("capture_geometry_unavailable")
    x, y, w, h, vw, vh = (geometry[k] for k in keys)
    if min(w, h, vw, vh) <= 0 or x + w > vw + 0.001 or y + h > vh + 0.001:
        raise CaptureError("capture_geometry_unavailable")
    pw, ph = size
    sx, sy = pw / vw, ph / vh
    if abs(pw - sy * vw) > 2 or abs(ph - sx * vh) > 2:
        raise CaptureError("capture_geometry_unavailable")
    l, t = floor(x * sx), floor(y * sy)
    r, b = min(pw, ceil((x + w) * sx)), min(ph, ceil((y + h) * sy))
    if not (l < r and t < b):
        raise CaptureError("capture_geometry_unavailable")
    return l, t, r - l, b - t


def crop_png(png, geometry):
    size = png_size(png)
    x, y, w, h = pixel_box(size, geometry)
    if (x, y, w, h) == (0, 0, *size):
        return png
    try:
        with TemporaryDirectory(prefix="agent-browser-capture-") as directory:
            source, destination = Path(directory) / "source.png", Path(directory) / "crop.png"
            source.write_bytes(png)
            subprocess.run(["/usr/bin/sips", "--cropToHeightWidth", str(h), str(w), "--cropOffset", str(y), str(x),
                            str(source), "--out", str(destination)], check=True, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with destination.open("rb") as cropped:
                result = cropped.read(MAX_IMAGE)
    except (OSError, subprocess.SubprocessError):
        raise CaptureError("capture_geometry_unavailable") from None
    if 4 * ((len(result) + 2) // 3) + len(PREFIX) > MAX_IMAGE or png_size(result) != (w, h):
        raise CaptureError("capture_geometry_unavailable")
    return result


def image_content(png):
    return {"type": "image", "mimeType": "image/png", "data": b64encode(png).decode("ascii")}
