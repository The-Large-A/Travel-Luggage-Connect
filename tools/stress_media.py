"""
Stress test on everyday-looking photos: every Commons photo is degraded with the
conditions in tools/realistic_capture.py (bus lighting, low light, blur, heavy
compression, tilt, half out of frame, glare, a busy background, a night bus) and
we measure what still works.

    python tools/stress_media.py            # needs datasets/media (python tools/fetch_media.py)

Columns
  colour   OpenCV main colour still in the photo's labelled colour family
  gate     the camera-scan frame gate still accepts the frame
  R@1      the same object's other (clean) photo is still CLIP's first choice
           among all photos of that kind — a passenger's bad photo vs the depot's
"""
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import cv2                                                     # noqa: E402
import mongomock                                               # noqa: E402

import realistic_capture as rc                                 # noqa: E402

MEDIA = os.path.join(ROOT, "datasets", "media")
FAMILY = {"navy blue": {"blue", "black"}, "blue": {"blue", "grey", "silver", "white"},
          "grey/brown": {"grey", "brown", "black", "silver"}, "red": {"red", "pink", "brown", "orange"},
          "black": {"black", "grey", "brown"}, "yellow": {"yellow", "gold", "orange"},
          "brown": {"brown", "orange", "gold", "black"}, "grey": {"grey", "silver", "black", "white"},
          "silver": {"silver", "grey", "white"}, "green": {"green", "grey"}, "teal": {"blue", "green", "grey"}}


def family(item):
    c = (item.get("colour") or "")
    return FAMILY.get(c) or FAMILY.get(c.split("/")[0].strip())


def load(tmp):
    items = [i for i in json.load(open(os.path.join(MEDIA, "manifest.json"), encoding="utf8"))["items"]
             if i["kind"] == "image" and not i.get("candid") and os.path.exists(os.path.join(MEDIA, i["file"]))]
    background = None
    clip = os.path.join(MEDIA, "files", "vid_03.webm")
    if os.path.exists(clip):
        cap = cv2.VideoCapture(clip)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 120)
        ok, frame = cap.read()
        background = frame if ok else None
    return items, background


def run(corrector=None):
    import tempfile
    from item_intake import vision
    from item_intake.config import IntakeConfig
    from similarity import ImageSimilarity
    cfg = IntakeConfig()
    tmp = tempfile.mkdtemp()
    items, background = load(tmp)
    clip = ImageSimilarity(db=mongomock.MongoClient()["stress"])
    clean = {}
    for i in items:
        p = os.path.join(tmp, f"{i['id']}.jpg")
        cv2.imwrite(p, cv2.imread(os.path.join(MEDIA, i["file"])))
        clean[i["id"]] = p
    conds = dict(rc.CONDITIONS)
    if background is not None:
        conds["cluttered"] = lambda img: rc.cluttered(img, background)
    by_cat = defaultdict(list)
    for i in items:
        by_cat[i["app_category"]].append(i)
    rows = {}
    for name, fn in [("clean", lambda x: x)] + list(conds.items()):
        col_ok = col_n = gate_ok = r_hit = r_n = 0
        for i in items:
            img = fn(cv2.imread(clean[i["id"]]))
            if corrector:
                img_for_colour = corrector(img)
            else:
                img_for_colour = img
            p = os.path.join(tmp, f"{i['id']}__{name}.jpg")
            cv2.imwrite(p, img)
            fam = family(i)
            if fam:
                small = vision.resize_max(img_for_colour, 640)
                cols = vision.dominant_colours(small, vision.item_mask(small))
                col_n += 1
                col_ok += bool(cols) and cols[0][0] in fam
            exp = vision.exposure(img)
            dark = exp["mean"] < cfg.min_brightness and (exp["dark"] > 0.25 or exp["mean"] < cfg.min_brightness / 2)
            gate_ok += vision.sharpness(img) >= cfg.min_sharpness and not dark and \
                not (exp["mean"] > cfg.max_brightness or exp["glare"] > 0.08)
            grp = i.get("same_object_group")
            if grp:
                pool = [o for o in by_cat[i["app_category"]] if o["id"] != i["id"]]
                best = max(pool, key=lambda o: clip.similarity(p, clean[o["id"]]))
                r_n += 1
                r_hit += best.get("same_object_group") == grp
        rows[name] = (col_ok, col_n, gate_ok, len(items), r_hit, r_n)
        print(f"{name:18} colour {col_ok:2}/{col_n:<2}  gate {gate_ok:2}/{len(items):<2}  R@1 {r_hit:2}/{r_n}",
              flush=True)
    return rows


if __name__ == "__main__":
    run()
