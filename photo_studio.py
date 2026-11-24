"""Product photo studio: turns a phone photo into a clean catalogue image.

Steps: fix the phone's rotation (EXIF), cut the product out of its background,
straighten boxes that were photographed at a slight angle, crop to the product,
center it on a white background with even margins, and gently even out the light.

Two outputs:
  - square (1000x1000) for the site
  - 16:9 (1000x562) for Wolt, which asks for a white background in that ratio
"""
import io
import threading

from PIL import Image, ImageOps, ImageFilter

_session = None
_session_lock = threading.Lock()
MODEL = "isnet-general-use"


def available():
    try:
        import rembg  # noqa: F401
        return True
    except Exception:
        return False


def _get_session():
    """The cut-out model is loaded once (the first call downloads it, ~180MB)."""
    global _session
    with _session_lock:
        if _session is None:
            from rembg import new_session
            _session = new_session(MODEL)
        return _session


def preload():
    """Load the model in the background at startup, so the first photo isn't slow."""
    if available():
        threading.Thread(target=lambda: _safe(_get_session), daemon=True).start()


def _safe(fn):
    try:
        fn()
    except Exception as e:
        print(f"[PHOTO] model preload failed: {e}")


def _cutout(img):
    """RGBA image with the background made transparent."""
    from rembg import remove
    return remove(img, session=_get_session(), post_process_mask=True)


def _straighten(rgba):
    """Rotate a box-like product that was photographed a little crooked.
    Only small angles (up to 15 degrees) and only clearly rectangular shapes, so a round
    cake tin or a bag is never rotated by mistake."""
    try:
        import numpy as np
        import cv2
    except Exception:
        return rgba
    alpha = np.array(rgba.split()[-1])
    mask = (alpha > 128).astype("uint8")
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return rgba
    c = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(c)
    (cx, cy), (w, h), angle = cv2.minAreaRect(c)
    if w * h == 0 or area / (w * h) < 0.88:      # not rectangular enough
        return rgba
    # minAreaRect angle: bring it into (-45, 45]
    if angle > 45:
        angle -= 90
    elif angle < -45:
        angle += 90
    if abs(angle) < 1 or abs(angle) > 15:
        return rgba
    return rgba.rotate(angle, resample=Image.BICUBIC, expand=True)


def _even_light(rgb, mask):
    """Brightens a dim photo without changing colours: one gain for all three channels,
    chosen so the brightest part of the product (measured on the product only) lands
    near white. Capped, so a bright photo is left alone and a dark one isn't blown out."""
    import numpy as np
    arr = np.asarray(rgb).astype("float32")
    sel = np.asarray(mask) > 0
    if sel.sum() < 100:
        return rgb
    lum = (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2])[sel]
    gain = min(1.25, max(1.0, 245.0 / max(float(np.percentile(lum, 99)), 1.0)))
    out = Image.fromarray(np.clip(arr * gain, 0, 255).astype("uint8"))
    return out.filter(ImageFilter.UnsharpMask(radius=1.2, percent=50, threshold=3))


def _place(product, size, fill=0.82):
    """Center the product on a white canvas, scaled to fill most of it."""
    W, H = size
    pw, ph = product.size
    scale = min(W * fill / pw, H * fill / ph)
    new = product.resize((max(1, int(pw * scale)), max(1, int(ph * scale))), Image.LANCZOS)
    canvas = Image.new("RGB", size, (255, 255, 255))
    canvas.paste(new, ((W - new.width) // 2, (H - new.height) // 2), new)
    return canvas


def process(data: bytes):
    """Returns {"square": jpeg_bytes, "wide": jpeg_bytes}. Raises on an unreadable image."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((2000, 2000))                    # phone photos are huge; this is plenty
    rgba = _cutout(img).convert("RGBA")
    rgba = _straighten(rgba)
    box = rgba.split()[-1].point(lambda a: 255 if a > 20 else 0).getbbox()
    if not box:
        raise ValueError("לא זוהה מוצר בתמונה")
    rgba = rgba.crop(box)
    alpha = rgba.split()[-1]
    rgb = _even_light(rgba.convert("RGB"), alpha.point(lambda a: 255 if a > 128 else 0))
    product = rgb.convert("RGBA")
    product.putalpha(alpha)

    out = {}
    for key, size in (("square", (1000, 1000)), ("wide", (1000, 562))):
        buf = io.BytesIO()
        _place(product, size).save(buf, "JPEG", quality=88, optimize=True, progressive=True)
        out[key] = buf.getvalue()
    return out
