"""The copy of a photo a model is sent (spec 02): at most 1568 px a side."""
from __future__ import annotations

import base64
import io

MODEL_SIDE = 1568


def for_model(image_b64: str | None) -> str | None:
    """Downscale to <=1568 px (JPEG q88). Small/unreadable images pass through."""
    if not image_b64:
        return image_b64
    try:
        from PIL import Image

        raw = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(raw))
        img.load()
        if max(img.size) <= MODEL_SIDE:
            return image_b64  # the same string, unchanged
        img = img.convert("RGB")
        img.thumbnail((MODEL_SIDE, MODEL_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return image_b64  # never raise on a photo we cannot read
