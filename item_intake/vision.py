"""
Classical computer vision for the depot smart-intake scan (OpenCV).

OpenCV decides which frames are worth keeping and reads pixel-level facts;
it does not recognise what the item is — that is CLIP / OCR / the captioner
(see analysis.py). Everything here is deterministic and CPU-cheap, so it runs
on every frame the browser sends (~2 per second).

    sharpness        variance of the Laplacian (low = blurred)
    exposure         mean L* (Lab) and the share of clipped pixels
    perceptual hash  64-bit DCT pHash; Hamming distance = how different a view is
    item mask        GrabCut seeded with a centre box (staff centre the item);
                     background subtraction is NOT used — it needs a still camera
    colours          k-means in Lab on item pixels, named by CIEDE2000
    text regions     gradient + Otsu + horizontal closing (classic text detector);
                     triggers the "hold the tag closer" prompt and ID masking
"""
import cv2
import numpy as np

from vocabulary import nearest_colour

ANALYSIS_EDGE = 480          # frames are analysed at this long edge, so thresholds are comparable


def decode(data: bytes):
    """JPEG/PNG bytes → BGR image, or None when it is not a decodable image."""
    if not data:
        return None
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.ndim != 3 or min(img.shape[:2]) < 32:
        return None
    return img


def resize_max(img, edge: int):
    h, w = img.shape[:2]
    scale = edge / max(h, w)
    if scale >= 1:
        return img
    return cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)


def sharpness(img) -> float:
    gray = cv2.cvtColor(resize_max(img, ANALYSIS_EDGE), cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def exposure(img) -> dict:
    """{'mean': L* 0-100, 'dark': share of near-black px, 'bright': share of blown-out px}."""
    lab = cv2.cvtColor(resize_max(img, ANALYSIS_EDGE), cv2.COLOR_BGR2LAB)
    L = lab[:, :, 0].astype(np.float32) * (100.0 / 255.0)
    return {"mean": float(L.mean()), "dark": float((L < 5).mean()), "bright": float((L > 98).mean())}


def phash(img) -> int:
    """64-bit perceptual hash: sign of the low-frequency DCT block vs its median."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    block = cv2.dct(small)[:8, :8]
    bits = (block > np.median(block.ravel()[1:])).ravel()      # skip the DC term for the median
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


def item_mask(img, iterations: int = 3):
    """
    Foreground mask (uint8 0/255, same size as img) from GrabCut seeded with the
    central 70% box. Falls back to a centred ellipse when GrabCut finds nothing
    sensible (plain item filling the frame, or a cluttered background).
    """
    small = resize_max(img, 320)
    h, w = small.shape[:2]
    rect = (int(w * 0.15), int(h * 0.15), int(w * 0.7), int(h * 0.7))
    mask = np.zeros((h, w), np.uint8)
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(small, mask, rect, bgd, fgd, iterations, cv2.GC_INIT_WITH_RECT)
        fg = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    except cv2.error:
        fg = np.zeros((h, w), np.uint8)
    share = fg.mean() / 255.0
    if not 0.05 <= share <= 0.95:
        fg = np.zeros((h, w), np.uint8)
        cv2.ellipse(fg, (w // 2, h // 2), (int(w * 0.35), int(h * 0.35)), 0, 0, 360, 255, -1)
    return cv2.resize(fg, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST)


def dominant_colours(img, mask=None, k: int = 4, sample: int = 6000, seed: int = 7) -> list:
    """
    Named colours of the item, largest first: [(name, share 0-1, (L, a, b))].
    k-means runs in Lab (perceptually uniform) on a deterministic pixel sample.
    """
    small = resize_max(img, 240)
    m = None if mask is None else cv2.resize(mask, (small.shape[1], small.shape[0]),
                                             interpolation=cv2.INTER_NEAREST)
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
    if m is not None:
        lab = lab[m.reshape(-1) > 0]
    if len(lab) < k * 10:
        return []
    rng = np.random.default_rng(seed)
    if len(lab) > sample:
        lab = lab[rng.choice(len(lab), sample, replace=False)]
    cv2.setRNGSeed(seed)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _, labels, centres = cv2.kmeans(lab, k, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.ravel(), minlength=k)
    named = {}
    for centre, count in zip(centres, counts):
        # OpenCV 8-bit Lab → CIE Lab
        L, a, b = centre[0] * 100.0 / 255.0, centre[1] - 128.0, centre[2] - 128.0
        name, _ = nearest_colour((L, a, b))
        share = float(count) / float(counts.sum())
        prev = named.get(name)
        named[name] = (share + (prev[0] if prev else 0.0), (L, a, b) if not prev or prev[0] < share else prev[1])
    return sorted(((n, round(float(s), 3), tuple(round(float(v), 1) for v in lab_c))
                   for n, (s, lab_c) in named.items()), key=lambda t: -t[1])


def text_regions(img, min_boxes: int = 1) -> list:
    """
    Likely printed-text regions as (x, y, w, h) boxes in the image's own pixels.
    Text is many small high-contrast strokes in a row: morphological gradient →
    Otsu threshold → close horizontally → keep wide, well-filled components.
    """
    small = resize_max(img, 640)
    scale = img.shape[1] / small.shape[1]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    connected = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 1)))
    contours, _ = cv2.findContours(connected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if h < 8 or h > small.shape[0] * 0.25 or w < 2.0 * h or w > small.shape[1] * 0.95:
            continue
        fill = cv2.countNonZero(bw[y:y + h, x:x + w]) / float(w * h)
        if fill < 0.30:
            continue
        boxes.append((int(x * scale), int(y * scale), int(w * scale), int(h * scale)))
    return boxes if len(boxes) >= min_boxes else []


def blur_regions(img, boxes, pad: int = 6):
    """Irreversibly pixelate + blur the given boxes (used to mask ID numbers before storage)."""
    out = img.copy()
    H, W = out.shape[:2]
    for x, y, w, h in boxes:
        x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(W, x + w + pad), min(H, y + h + pad)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        roi = out[y0:y1, x0:x1]
        tiny = cv2.resize(roi, (max(1, roi.shape[1] // 10), max(1, roi.shape[0] // 10)), interpolation=cv2.INTER_AREA)
        coarse = cv2.resize(tiny, (roi.shape[1], roi.shape[0]), interpolation=cv2.INTER_NEAREST)
        out[y0:y1, x0:x1] = cv2.GaussianBlur(coarse, (9, 9), 0)
    return out


def centre_crop(img, frac: float = 0.5):
    h, w = img.shape[:2]
    ch, cw = int(h * frac), int(w * frac)
    y, x = (h - ch) // 2, (w - cw) // 2
    return img[y:y + ch, x:x + cw]


def encode_jpeg(img, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return buf.tobytes()
