"""
Make clean photos look like real ones: what a passenger snaps at night on a bus,
or a depot clerk shoots on a cluttered counter. Deterministic (seeded), so the
stress results in tools/stress_media.py and tests/test_media.py are repeatable.

Each condition is a function img(BGR uint8) -> img. COMBOS chain them.
"""
import cv2
import numpy as np


def _rng(seed=7):
    return np.random.default_rng(seed)


def yellow_bus_light(img):
    """Sodium / warm tungsten interior: strong yellow-orange cast."""
    out = img.astype(np.float32)
    out[..., 0] *= 0.62          # blue down
    out[..., 1] *= 0.95
    out[..., 2] *= 1.18          # red up
    return np.clip(out, 0, 255).astype(np.uint8)


def tube_light(img):
    """Cheap fluorescent tube: green-cyan cast, a bit flat."""
    out = img.astype(np.float32)
    out[..., 0] *= 1.05
    out[..., 1] *= 1.15
    out[..., 2] *= 0.82
    out = 0.85 * out + 20
    return np.clip(out, 0, 255).astype(np.uint8)


def low_light(img, seed=7):
    """Dim interior: dark, crushed, with phone sensor noise."""
    out = (img.astype(np.float32) / 255.0) ** 1.9 * 255 * 0.55
    out += _rng(seed).normal(0, 9, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


def motion_blur(img, length=17):
    k = np.zeros((length, length), np.float32)
    k[length // 2, :] = 1.0 / length
    return cv2.filter2D(img, -1, k)


def defocus(img):
    return cv2.GaussianBlur(img, (0, 0), 3.2)


def whatsapp_jpeg(img):
    """Downscaled and re-compressed hard, as photos forwarded on messaging apps are."""
    h, w = img.shape[:2]
    s = 640 / max(h, w)
    small = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else img
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 18])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def tilted(img):
    """Hand-held at an angle: rotation plus a perspective skew."""
    h, w = img.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[w * 0.08, h * 0.12], [w * 0.95, 0], [w * 0.88, h * 0.9], [0, h * 0.97]])
    m = cv2.getPerspectiveTransform(src, dst)
    out = cv2.warpPerspective(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE)
    rot = cv2.getRotationMatrix2D((w / 2, h / 2), 18, 1.0)
    return cv2.warpAffine(out, rot, (w, h), borderMode=cv2.BORDER_REPLICATE)


def partial(img):
    """The item half out of frame."""
    h, w = img.shape[:2]
    return img[int(h * 0.25):, int(w * 0.38):].copy()


def glare(img):
    """A window or flash reflection on the item."""
    out = img.copy()
    h, w = img.shape[:2]
    over = out.copy()
    cv2.ellipse(over, (int(w * 0.45), int(h * 0.4)), (int(w * 0.16), int(h * 0.09)), 20, 0, 360,
                (255, 255, 255), -1)
    over = cv2.GaussianBlur(over, (0, 0), 9)
    return cv2.addWeighted(out, 0.35, over, 0.65, 0)


def cluttered(img, background):
    """The item, cut out with GrabCut, placed small in a busy real scene."""
    from item_intake import vision
    item = vision.resize_max(img, 480)
    mask = vision.item_mask(item)
    bg = cv2.resize(background, (900, 675))
    ih, iw = item.shape[:2]
    scale = 0.45 * min(bg.shape[1] / iw, bg.shape[0] / ih)
    item = cv2.resize(item, (int(iw * scale), int(ih * scale)))
    mask = cv2.resize(mask, (item.shape[1], item.shape[0]), interpolation=cv2.INTER_NEAREST)
    y, x = int(bg.shape[0] * 0.4), int(bg.shape[1] * 0.3)
    roi = bg[y:y + item.shape[0], x:x + item.shape[1]]
    m = (mask[:roi.shape[0], :roi.shape[1]] > 0)[..., None]
    roi[:] = np.where(m, item[:roi.shape[0], :roi.shape[1]], roi)
    return bg


def night_bus(img):
    """The worst common case: dim, yellow, noisy, shaken and forwarded."""
    return whatsapp_jpeg(motion_blur(low_light(yellow_bus_light(img)), 9))


CONDITIONS = {
    "yellow_bus_light": yellow_bus_light, "tube_light": tube_light, "low_light": low_light,
    "motion_blur": motion_blur, "defocus": defocus, "whatsapp_jpeg": whatsapp_jpeg, "tilted": tilted,
    "partial": partial, "glare": glare, "night_bus": night_bus,
}
