import io

import pypdfium2 as pdfium
from PIL import Image

from plan_cuidado.adapters.cache import FileOcrCache
from plan_cuidado.adapters.pdf import PdfiumRenderer, orient_landscape


def _pdf(sizes) -> bytes:
    doc = pdfium.PdfDocument.new()
    for w, h in sizes:
        doc.new_page(w, h)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _size(img_bytes):
    return Image.open(io.BytesIO(img_bytes)).size


def test_renderer_outputs_landscape_jpeg_pages():
    r = PdfiumRenderer(scale=0.5)
    pages = r.render_pages(_pdf([(842, 595), (595, 842)]))
    assert len(pages) == 2
    for img in pages:
        w, h = _size(img)
        assert w > h
        assert Image.open(io.BytesIO(img)).format == "JPEG"


def test_orient_landscape_rotates_portrait_clockwise():
    img = Image.new("RGB", (100, 200), "white")
    img.putpixel((0, 0), (255, 0, 0))  # top-left marker
    out = orient_landscape(img)
    assert out.size == (200, 100)
    # clockwise: top-left goes to top-right
    assert out.getpixel((199, 0))[0] > 200


def test_rotate_bytes():
    r = PdfiumRenderer()
    buf = io.BytesIO()
    Image.new("RGB", (300, 100)).save(buf, format="JPEG")
    assert _size(r.rotate(buf.getvalue(), 90)) == (100, 300)
    assert _size(r.rotate(buf.getvalue(), 180)) == (300, 100)


def test_file_cache_roundtrip(tmp_path):
    c = FileOcrCache(tmp_path)
    assert c.get("F1", 1, "google/gemini-2.5-flash-lite") is None
    c.put("F1", 1, "google/gemini-2.5-flash-lite", {"content": "{}", "usage": {"cost": 1}})
    assert c.get("F1", 1, "google/gemini-2.5-flash-lite") == {"content": "{}", "usage": {"cost": 1}}
    assert c.get("F1", 1, "google/gemini-2.5-flash-lite", variant="rot90") is None
    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1 and ":" not in files[0].name and "/" not in files[0].name


def _jpeg(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="JPEG")
    return buf.getvalue()


def test_crop_keeps_full_width_band_and_limits_longest_side():
    r = PdfiumRenderer()
    img = _jpeg(3000, 2000)
    assert _size(r.crop(img, 0.0, 1.0, 1500)) == (1500, 1000)  # header: whole page downscaled
    w, h = _size(r.crop(img, 0.25, 0.5, 2400))
    assert w == 2400 and h == 400  # 3000x500 band scaled to max side 2400
    assert _size(r.crop(img, 0.25, 0.5, 5000)) == (3000, 500)  # never upscaled


def test_render_scale_sets_resolution():
    w, _ = _size(PdfiumRenderer(scale=2.0).render_pages(_pdf([(842, 595)]))[0])
    assert w == 1684


def test_file_cache_clear_per_file(tmp_path):
    c = FileOcrCache(tmp_path)
    c.put("F1", 1, "m", {"content": "{}"}, variant="header")
    c.put("F1", 1, "m", {"content": "{}"}, variant="strip1of3")
    c.put("F2", 1, "m", {"content": "{}"})
    assert c.clear("F1") == 2
    assert c.get("F1", 1, "m", variant="header") is None
    assert c.get("F2", 1, "m") is not None
    assert c.clear("missing") == 0


def test_crop_can_narrow_to_the_table_columns_and_paste_the_titles_on_top():
    r = PdfiumRenderer()
    img = _jpeg(3000, 2000)
    # band 0.5-0.6 (200 px) of columns 0.1-0.9 (2400 px) under the titles band 0.1-0.15 (100 px)
    w, h = _size(r.crop(img, 0.5, 0.6, 5000, left=0.1, right=0.9, prepend=(0.1, 0.15)))
    assert w == 2400 and h == 200 + 100 + PdfiumRenderer.TITLES_GAP


def test_file_cache_writes_are_atomic_under_concurrent_puts(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    c = FileOcrCache(tmp_path)
    payloads = [{"content": "x" * 50_000, "n": i} for i in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda d: c.put("F1", 1, "m", d, "same"), payloads))
    assert c.get("F1", 1, "m", "same") in payloads  # never a torn file
    assert [p.name for p in (tmp_path / "F1").iterdir()] == ["p1_m_same.json"]  # no temp files left


def test_file_cache_put_retries_when_windows_denies_the_rename(tmp_path, monkeypatch):
    # Windows refuses to replace a file another thread is renaming over or reading (WinError 5).
    import plan_cuidado.adapters.cache as cache_module

    real_replace = cache_module.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(cache_module.os, "replace", flaky_replace)
    monkeypatch.setattr(cache_module.time, "sleep", lambda _s: None)
    c = FileOcrCache(tmp_path)
    c.put("F1", 1, "m", {"ok": True})
    assert c.get("F1", 1, "m") == {"ok": True}
    assert [p.name for p in (tmp_path / "F1").iterdir()] == ["p1_m.json"]
