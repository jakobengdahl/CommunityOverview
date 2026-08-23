"""Tests for backend/core/image_ingest.py: decode/fetch/optimize for embedded
`image` annotations. Session-level budget enforcement (per-session and
per-document byte caps) is covered separately in
backend/core/tests/test_session_manager.py and the MCP-facing tool in
backend/service/tests/test_mcp_image_annotation_tool.py.
"""

import base64
import io

import httpx2 as httpx
import pytest
from PIL import Image

from backend.core import image_ingest


def _png_bytes(size=(4, 4), color=(255, 0, 0), mode="RGB"):
    image = Image.new(mode, size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes(size=(4, 4), color=(0, 255, 0)):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _webp_bytes(size=(4, 4), color=(0, 0, 255)):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP")
    return buffer.getvalue()


def _bmp_bytes(size=(4, 4)):
    image = Image.new("RGB", size, (1, 2, 3))
    buffer = io.BytesIO()
    image.save(buffer, format="BMP")
    return buffer.getvalue()


class TestDecodeImageData:
    def test_decodes_data_url(self):
        raw = _png_bytes()
        data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        assert image_ingest.decode_image_data(data_url) == raw

    def test_decodes_bare_base64(self):
        raw = _png_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        assert image_ingest.decode_image_data(encoded) == raw

    def test_rejects_non_base64_data_url(self):
        with pytest.raises(image_ingest.InvalidImageData):
            image_ingest.decode_image_data("data:image/png,not-base64-marked")

    def test_rejects_invalid_base64(self):
        with pytest.raises(image_ingest.InvalidImageData):
            image_ingest.decode_image_data("not valid base64 !!!")

    def test_rejects_empty_string(self):
        with pytest.raises(image_ingest.InvalidImageData):
            image_ingest.decode_image_data("")

    def test_rejects_non_string(self):
        with pytest.raises(image_ingest.InvalidImageData):
            image_ingest.decode_image_data(None)  # type: ignore[arg-type]


class TestOptimizeImageFormats:
    @pytest.mark.parametrize(
        "builder", [_png_bytes, _jpeg_bytes, _webp_bytes], ids=["png", "jpeg", "webp"]
    )
    def test_accepts_supported_formats(self, builder):
        result = image_ingest.optimize_image(builder())
        assert result.content_type == "image/webp"
        assert result.width == 4
        assert result.height == 4
        assert result.data_url.startswith("data:image/webp;base64,")

    def test_rejects_unsupported_format(self):
        with pytest.raises(image_ingest.UnsupportedImageType):
            image_ingest.optimize_image(_bmp_bytes())

    def test_rejects_undecodable_bytes(self):
        with pytest.raises(image_ingest.InvalidImageData):
            image_ingest.optimize_image(b"this is not an image")


class TestOptimizeImageDownscaling:
    def test_downscales_to_max_longest_side(self):
        raw = _png_bytes(size=(4000, 2000))
        result = image_ingest.optimize_image(raw)
        assert max(result.width, result.height) == image_ingest.MAX_LONGEST_SIDE
        assert result.width == image_ingest.MAX_LONGEST_SIDE
        assert result.height == image_ingest.MAX_LONGEST_SIDE // 2

    def test_leaves_small_image_dimensions_unchanged(self):
        raw = _png_bytes(size=(100, 50))
        result = image_ingest.optimize_image(raw)
        assert (result.width, result.height) == (100, 50)


class TestOptimizeImageTransparency:
    def test_preserves_png_alpha_channel(self):
        raw = _png_bytes(size=(4, 4), color=(10, 20, 30, 128), mode="RGBA")
        result = image_ingest.optimize_image(raw)
        decoded = Image.open(io.BytesIO(result.data))
        assert decoded.mode in ("RGBA", "LA")
        assert decoded.getpixel((0, 0))[3] < 255  # alpha channel survived

    def test_opaque_source_has_no_alpha(self):
        raw = _jpeg_bytes()
        result = image_ingest.optimize_image(raw)
        decoded = Image.open(io.BytesIO(result.data))
        assert decoded.mode == "RGB"


class TestOptimizeImageSizeCap:
    def test_raises_when_still_too_large_after_downscaling(self):
        raw = _png_bytes(size=(2000, 2000))
        with pytest.raises(image_ingest.OptimizedImageTooLarge):
            image_ingest.optimize_image(raw, max_optimized_bytes=16)

    def test_default_cap_allows_a_small_image(self):
        raw = _png_bytes(size=(200, 200))
        result = image_ingest.optimize_image(raw)
        assert len(result.data) <= image_ingest.DEFAULT_MAX_OPTIMIZED_IMAGE_BYTES


class _StubHttpxModule:
    """Stands in for the module-level `httpx` name in image_ingest.py.

    `fetch_image_bytes` constructs its own `httpx.Client()`; replacing the
    module attribute is the only way to bind a mock transport without
    opening a real socket (mirrors federation/tests/test_manager.py).
    """

    HTTPError = httpx.HTTPError

    def __init__(self, transport):
        self._transport = transport

    def Client(self, **kwargs):
        return httpx.Client(transport=self._transport, **kwargs)


def _install_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(image_ingest, "httpx", _StubHttpxModule(transport))


class TestFetchImageBytes:
    def test_fetches_and_returns_body(self, monkeypatch):
        raw = _png_bytes()

        def handler(request):
            return httpx.Response(200, content=raw)

        _install_transport(monkeypatch, handler)
        result = image_ingest.fetch_image_bytes("https://example.invalid/pic.png")
        assert result == raw

    def test_rejects_non_http_scheme(self, monkeypatch):
        with pytest.raises(image_ingest.ImageFetchError):
            image_ingest.fetch_image_bytes("file:///etc/passwd")

    def test_raises_on_http_error_status(self, monkeypatch):
        def handler(request):
            return httpx.Response(404, content=b"not found")

        _install_transport(monkeypatch, handler)
        with pytest.raises(image_ingest.ImageFetchError):
            image_ingest.fetch_image_bytes("https://example.invalid/missing.png")

    def test_raises_on_transport_error(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        _install_transport(monkeypatch, handler)
        with pytest.raises(image_ingest.ImageFetchError):
            image_ingest.fetch_image_bytes("https://example.invalid/pic.png")

    def test_raises_when_body_exceeds_max_bytes(self, monkeypatch):
        raw = b"x" * 1000

        def handler(request):
            return httpx.Response(200, content=raw)

        _install_transport(monkeypatch, handler)
        with pytest.raises(image_ingest.SourceImageTooLarge):
            image_ingest.fetch_image_bytes(
                "https://example.invalid/big.png", max_bytes=10
            )

    def test_raises_on_empty_body(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, content=b"")

        _install_transport(monkeypatch, handler)
        with pytest.raises(image_ingest.ImageFetchError):
            image_ingest.fetch_image_bytes("https://example.invalid/empty.png")


class TestDataUrlByteLength:
    def test_zero_for_non_data_url(self):
        assert image_ingest.data_url_byte_length("https://example.com/x.png") == 0

    def test_zero_for_non_string(self):
        assert image_ingest.data_url_byte_length(None) == 0

    def test_approximates_decoded_length(self):
        raw = b"x" * 300
        encoded = base64.b64encode(raw).decode("ascii")
        url = f"data:image/webp;base64,{encoded}"
        # Padding-agnostic approximation; exact for input with no padding.
        assert image_ingest.data_url_byte_length(url) == len(raw)
