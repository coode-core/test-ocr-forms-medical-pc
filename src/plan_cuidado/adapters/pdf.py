"""PDF -> page images with pypdfium2 (no system dependencies)."""

from __future__ import annotations

import io

import pypdfium2 as pdfium
from PIL import Image


def orient_landscape(image: Image.Image) -> Image.Image:
    """The planilla is a landscape table. CamScanner pages that come out portrait
    are, in practice, rotated 90° counter-clockwise, so rotate them clockwise.
    The OCR model reports any remaining rotation and the page is retried."""
    if image.height > image.width:
        return image.rotate(-90, expand=True)
    return image


def _to_jpeg(image: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class PdfiumRenderer:
    """Renders pages at high resolution; `crop` produces the (smaller) images sent to the model."""

    def __init__(self, scale: float = 4.0, max_side: int = 6000):
        self.scale = scale
        self.max_side = max_side

    def render_pages(self, pdf: bytes) -> list[bytes]:
        doc = pdfium.PdfDocument(pdf)
        try:
            images = []
            for page in doc:
                # render() honours the page /Rotate entry
                image = page.render(scale=self.scale).to_pil()
                image = orient_landscape(image)
                image.thumbnail((self.max_side, self.max_side))
                images.append(_to_jpeg(image))
            return images
        finally:
            doc.close()

    def rotate(self, image: bytes, degrees_clockwise: int) -> bytes:
        pil = Image.open(io.BytesIO(image))
        return _to_jpeg(pil.rotate(-degrees_clockwise, expand=True))

    TITLES_GAP = 8  # white pixels between the pasted column titles and the rows

    def crop(
        self, image: bytes, top: float, bottom: float, max_side: int,
        left: float = 0.0, right: float = 1.0, prepend: tuple[float, float] | None = None,
    ) -> bytes:
        """Band [top, bottom] x [left, right] (fractions of the page), with the band `prepend`
        (y0, y1) of the same columns pasted above it, downscaled so its longest side is at most
        `max_side` (never upscaled)."""
        pil = Image.open(io.BytesIO(image))
        width, height = pil.size
        x0 = max(0, min(width - 1, round(left * width)))
        x1 = max(x0 + 1, min(width, round(right * width)))

        def band(a: float, b: float) -> Image.Image:
            y0 = max(0, min(height - 1, round(a * height)))
            y1 = max(y0 + 1, min(height, round(b * height)))
            return pil.crop((x0, y0, x1, y1))

        out = band(top, bottom)
        if prepend:
            titles = band(*prepend)
            sheet = Image.new("RGB", (out.width, titles.height + self.TITLES_GAP + out.height), "white")
            sheet.paste(titles, (0, 0))
            sheet.paste(out, (0, titles.height + self.TITLES_GAP))
            out = sheet
        out.thumbnail((max_side, max_side))
        return _to_jpeg(out)
