"""Server-side ingestion for embedded `image` annotations.

An `image` annotation embeds its picture as a base64 data URI in
``content.image.url`` (see ``session_annotations.py`` and
``GenericAnnotationNode.jsx``'s ``<img src=...>``) — never a remote link, so
the annotation still renders after the source disappears. This module owns
the ingest step shared by both sources an MCP/API caller can supply:

* raw image bytes (a data URL or bare base64 string), or
* a URL, fetched exactly once here and turned into the same embedded copy a
  direct upload would produce (``fetch_image_bytes``).

Either way the bytes are decoded, validated and re-encoded by
``optimize_image`` before anything is stored. Format validation is done by
attempting to decode the bytes with Pillow and reading back the format it
detected — not by trusting a caller- or server-declared content-type header,
which can be wrong or spoofed.

Session-level budgets (total embedded-image bytes per session, total
document bytes) are enforced by ``SessionManager.upsert_image_annotation``
once the optimized size here is known; this module only knows about one
image at a time.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import httpx2 as httpx
from PIL import Image, UnidentifiedImageError

# SVG/GIF/etc are rejected: SVG can carry script content, GIF animation is
# not preserved by the optimizer (a re-encode would silently drop frames).
_PIL_FORMAT_TO_CONTENT_TYPE = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}
ALLOWED_CONTENT_TYPES = frozenset(_PIL_FORMAT_TO_CONTENT_TYPE.values())

# Sanity cap on the raw source (upload or download) before optimization runs,
# independent of the post-optimization per-image budget below — without this
# a multi-hundred-MB source would still be fully decoded into memory.
DEFAULT_MAX_SOURCE_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_OPTIMIZED_IMAGE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_SESSION_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_SESSION_DOCUMENT_BYTES = 25 * 1024 * 1024

MAX_LONGEST_SIDE = 2560
WEBP_QUALITY = 82
_FETCH_TIMEOUT_SECONDS = 10.0


class ImageIngestError(ValueError):
    """Base for all image-ingest validation/fetch failures."""


class InvalidImageData(ImageIngestError):
    """The supplied bytes are not valid base64, or not a decodable image."""


class UnsupportedImageType(ImageIngestError):
    """The decoded image is a real image but not PNG/JPEG/WebP."""


class SourceImageTooLarge(ImageIngestError):
    """The raw source (before optimization) exceeds the sanity cap."""


class OptimizedImageTooLarge(ImageIngestError):
    """The image still exceeds the per-image budget after downscaling."""


class ImageFetchError(ImageIngestError):
    """Fetching `image_url` failed (network error, non-2xx, bad scheme)."""


@dataclass(frozen=True)
class OptimizedImage:
    data: bytes
    content_type: str
    width: int
    height: int

    @property
    def data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.content_type};base64,{encoded}"


def decode_image_data(image_data: str) -> bytes:
    """Decode a ``data:<type>;base64,<...>`` string or bare base64 into bytes.

    Any declared content-type in a data URL header is ignored — the real
    type is re-derived from the decoded bytes by ``optimize_image``.
    """
    if not isinstance(image_data, str) or not image_data:
        raise InvalidImageData("image_data must be a non-empty string")
    payload = image_data
    if image_data.startswith("data:"):
        header, sep, rest = image_data.partition(",")
        if not sep or ";base64" not in header:
            raise InvalidImageData("data URL must be base64-encoded")
        payload = rest
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError) as exc:
        raise InvalidImageData("image_data is not valid base64") from exc
    if not raw:
        raise InvalidImageData("image_data decoded to zero bytes")
    return raw


def fetch_image_bytes(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_SOURCE_IMAGE_BYTES,
    timeout: float = _FETCH_TIMEOUT_SECONDS,
) -> bytes:
    """Fetch *url* once, server-side, streaming with a byte cap.

    The caller stores the bytes this returns (after ``optimize_image``) as
    an embedded copy — the URL itself is never persisted or re-served, so a
    dead or blocked source afterwards does not break the annotation.
    """
    if not isinstance(url, str) or not (
        url.startswith("http://") or url.startswith("https://")
    ):
        raise ImageFetchError("image_url must be an http(s) URL")
    try:
        # A `Client` (rather than the bare module-level `httpx.stream`) is used
        # so tests can substitute a mock transport by monkeypatching the
        # module-level `httpx` name, the same pattern federation/manager.py
        # uses for its AsyncClient.
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise SourceImageTooLarge(
                            f"downloaded image exceeds {max_bytes} bytes "
                            "before optimization"
                        )
                    chunks.append(chunk)
    except SourceImageTooLarge:
        raise
    except httpx.HTTPError as exc:
        raise ImageFetchError(f"failed to fetch image_url: {exc}") from exc
    data = b"".join(chunks)
    if not data:
        raise ImageFetchError("image_url returned an empty body")
    return data


def optimize_image(
    raw: bytes,
    *,
    max_optimized_bytes: int = DEFAULT_MAX_OPTIMIZED_IMAGE_BYTES,
) -> OptimizedImage:
    """Validate, downscale and re-encode *raw* image bytes.

    Decodes with Pillow, which sniffs the real format from the bytes rather
    than trusting a declared content-type, and rejects anything that is not
    PNG/JPEG/WebP. Downscales (preserving aspect ratio) so the longest side
    is at most ``MAX_LONGEST_SIDE``, then re-encodes as WebP at
    ``WEBP_QUALITY`` — WebP carries an alpha channel natively, so PNG/WebP
    source transparency survives the re-encode (a flattened JPEG source
    never had transparency to preserve). Raises ``OptimizedImageTooLarge``
    if the result still exceeds *max_optimized_bytes* after downscaling.
    """
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageData(f"could not decode image data: {exc}") from exc

    source_format = (image.format or "").upper()
    if source_format not in _PIL_FORMAT_TO_CONTENT_TYPE:
        raise UnsupportedImageType(
            f"unsupported image format {source_format or 'unknown'!r}; "
            "only PNG, JPEG and WebP are accepted"
        )

    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )
    image = image.convert("RGBA") if has_alpha else image.convert("RGB")

    width, height = image.size
    longest = max(width, height)
    if longest > MAX_LONGEST_SIDE:
        scale = MAX_LONGEST_SIDE / longest
        width = max(1, round(width * scale))
        height = max(1, round(height * scale))
        image = image.resize((width, height), Image.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=6)
    data = buffer.getvalue()
    if len(data) > max_optimized_bytes:
        raise OptimizedImageTooLarge(
            f"optimized image is {len(data)} bytes, exceeding the "
            f"{max_optimized_bytes}-byte limit even after downscaling to "
            f"{width}x{height}"
        )
    return OptimizedImage(
        data=data, content_type="image/webp", width=width, height=height
    )


def data_url_byte_length(url: object) -> int:
    """Approximate decoded byte length of a ``data:...;base64,<...>`` string.

    Padding-agnostic (over by at most 2 bytes for padded input) — good
    enough for a soft budget check without base64-decoding every embedded
    image on every annotation write.
    """
    if not isinstance(url, str) or ";base64," not in url:
        return 0
    _, _, encoded = url.partition(";base64,")
    return (len(encoded) * 3) // 4
