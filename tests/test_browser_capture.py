from base64 import b64decode, b64encode
import struct
import unittest
from zlib import crc32

from browser_capture import CaptureError, PREFIX, collect, pixel_box, png_size


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9ZkAAAAASUVORK5CYII="


class CaptureTest(unittest.TestCase):
    def test_collects_ordered_chunks_across_frame_boundaries(self):
        original = b64decode(PNG)
        # Add a valid ancillary chunk so this fixture is larger than one frame payload.
        payload = b"tEXt" + b"Sample\x00" + b"x" * 96000
        png = original[:-12] + struct.pack(">I", len(payload) - 4) + payload + struct.pack(">I", crc32(payload)) + original[-12:]
        image = PREFIX + b64encode(png).decode()
        seen = []

        def request(operation, args, timeout):
            self.assertEqual(operation, "capture_chunk")
            self.assertEqual(args["image_ref"], "a" * 24)
            offset = args["offset"]
            seen.append(offset)
            data = image[offset:offset + 48000]
            return dict(status="ok", data=data, next_offset=offset + len(data))

        self.assertEqual(collect(dict(image_ref="a" * 24, image_length=len(image)), request, lambda: 0), png)
        self.assertEqual(seen, [0, 48000, 96000])

    def test_rejects_invalid_transfer_and_png(self):
        begin = dict(image_ref="a" * 24, image_length=50000)
        with self.assertRaisesRegex(CaptureError, "read_unavailable"):
            collect(begin, lambda *_: dict(status="ok", data="A" * 20000, next_offset=1), lambda: 0)
        with self.assertRaisesRegex(CaptureError, "capture_expired"):
            collect(begin, lambda *_: dict(status="capture_expired"), lambda: 0)
        with self.assertRaisesRegex(CaptureError, "read_unavailable"):
            png_size(b"\x89PNG\r\n\x1a\n")

    def test_pixel_box_handles_fractional_and_rejects_mismatched_raster(self):
        geometry = dict(x=2.5, y=3.5, width=3.75, height=9, viewport_width=100, viewport_height=200)
        self.assertEqual(pixel_box((200, 400), geometry), (5, 7, 8, 18))
        with self.assertRaisesRegex(CaptureError, "capture_geometry_unavailable"):
            pixel_box((200, 500), geometry)
        with self.assertRaisesRegex(CaptureError, "capture_geometry_unavailable"):
            pixel_box((200, 400), dict(geometry, x=float("nan")))
