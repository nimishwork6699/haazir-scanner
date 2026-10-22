"""CAPTCHA reader for eCourts' 6-character Securimage images.

bharat-courts' default solver runs one ddddocr model and often reads only 4-5 characters,
which wastes the attempt (the portal needs exactly 6). We restrict output to letters and
digits and try a few readings in order, returning the first that has exactly 6 characters.
"""

import io
import logging
import re

import ddddocr
from bharat_courts.captcha.base import CaptchaSolver
from PIL import Image, ImageFilter

log = logging.getLogger(__name__)
_ALNUM = 6  # ddddocr range: a-z, A-Z, 0-9


def _clean(img_bytes: bytes, threshold: int) -> bytes:
    img = Image.open(io.BytesIO(img_bytes)).convert("L")
    img = img.point(lambda x: 255 if x > threshold else 0).filter(ImageFilter.MedianFilter(size=3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class MultiReadSolver(CaptchaSolver):
    def __init__(self) -> None:
        self.models = []
        for beta in (False, True):
            m = ddddocr.DdddOcr(beta=beta, show_ad=False)
            m.set_ranges(_ALNUM)
            self.models.append(m)

    def _read(self, model, img: bytes) -> str:
        out = model.classification(img)
        return re.sub(r"[^A-Za-z0-9]", "", out if isinstance(out, str) else "")

    async def solve(self, image_bytes: bytes) -> str:
        # Most CAPTCHAs are 6 characters but some are 5. A 6-character read wins outright;
        # a 5-character read is used when both models agree on it.
        variants = [image_bytes, _clean(image_bytes, 140), _clean(image_bytes, 110)]
        seen = []
        for img in variants:
            reads = [self._read(m, img) for m in self.models]
            seen += reads
            six = next((r for r in reads if len(r) == 6), None)
            if six:
                return six
            if len(reads[0]) == 5 and reads[0].lower() == reads[1].lower():
                return reads[0]
        log.warning("CAPTCHA unreadable, got %s", seen)
        return ""
