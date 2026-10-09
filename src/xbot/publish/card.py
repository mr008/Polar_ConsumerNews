"""Image card for the image_card experiment (spec 2026-10-08 §2).

Draws the post's OWN text (hook large, the other lines below, our handle in
the footer) as a 1200x675 PNG. No source media, no generated imagery. Pillow
is an optional extra (`pip install -e ".[media]"`), imported lazily: without
it render_card raises ImportError and the publisher posts plain text.
"""
from __future__ import annotations

W, H = 1200, 675
_BG, _FG, _DIM = (16, 20, 24), (244, 246, 243), (154, 165, 173)
_PAD = 72


def card_lines(commentary: str) -> tuple[str, list[str]]:
    """(hook, other lines) from a commentary body. Drops the h/t tail and any
    line that addresses a handle (the author_bait question stays text-only)."""
    from .publisher import strip_ht_tail
    lines = [ln.strip() for ln in strip_ht_tail(commentary or "").splitlines()
             if ln.strip()]
    if not lines:
        return "", []
    return lines[0], [ln for ln in lines[1:] if "@" not in ln]


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    out, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    return out


def render_card(commentary: str, handle: str) -> bytes:
    """PNG bytes of the card. Raises ImportError when Pillow is missing."""
    import io
    from PIL import Image, ImageDraw, ImageFont  # lazy: optional extra

    hook, rest = card_lines(commentary)
    img = Image.new("RGB", (W, H), _BG)
    d = ImageDraw.Draw(img)
    big = ImageFont.load_default(size=56)
    small = ImageFont.load_default(size=34)
    foot = ImageFont.load_default(size=26)
    y, max_w, floor = _PAD, W - 2 * _PAD, H - 110
    for ln in _wrap(d, hook, big, max_w)[:3]:
        d.text((_PAD, y), ln, font=big, fill=_FG)
        y += 68
    y += 20
    for para in rest:
        for ln in _wrap(d, para, small, max_w):
            if y > floor:
                break
            d.text((_PAD, y), ln, font=small, fill=_FG)
            y += 44
        y += 16
    if handle:
        d.text((_PAD, H - 60), f"@{handle.lstrip('@')}", font=foot, fill=_DIM)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
