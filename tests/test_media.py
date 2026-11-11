"""
Real photos and videos (datasets/media, Wikimedia Commons; fetch with
`python tools/fetch_media.py`) through the REAL models: CLIP for photo and
description↔photo similarity and zero-shot categories, OpenCV for colours and
the camera-scan frame gate, and the v2 matcher on top.

Skipped when the media has not been downloaded or CLIP cannot load, so the
normal suite stays fast and offline. Runs in about a minute on a laptop CPU.
"""
import io
import json
import os
from collections import defaultdict
from statistics import mean, pstdev

import cv2
import mongomock
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA = os.path.join(ROOT, "datasets", "media")
MANIFEST = os.path.join(MEDIA, "manifest.json")


def _items():
    if not os.path.exists(MANIFEST):
        return []
    items = json.load(open(MANIFEST, encoding="utf8"))["items"]
    return [i for i in items if os.path.exists(os.path.join(MEDIA, i["file"]))]


ITEMS = _items()
IMAGES = [i for i in ITEMS if i["kind"] == "image"]
VIDEOS = [i for i in ITEMS if i["kind"] == "video"]
pytestmark = pytest.mark.skipif(len(IMAGES) < 30, reason="test media not downloaded (python tools/fetch_media.py)")


def path(item):
    return os.path.join(MEDIA, item["file"])


def read_image(item):
    img = cv2.imread(path(item))
    if img is None:                                    # e.g. a format OpenCV does not read
        from PIL import Image
        img = cv2.cvtColor(np.array(Image.open(path(item)).convert("RGB")), cv2.COLOR_RGB2BGR)
    return img


def video_frames(item, every=1, limit=120):
    """Frames of a webm (OpenCV / FFmpeg) or an animated GIF (Pillow)."""
    frames = []
    if item["file"].endswith(".gif"):
        from PIL import Image, ImageSequence
        with Image.open(path(item)) as gif:
            for n, frame in enumerate(ImageSequence.Iterator(gif)):
                if n % every == 0:
                    frames.append(cv2.cvtColor(np.array(frame.convert("RGB")), cv2.COLOR_RGB2BGR))
                if len(frames) >= limit:
                    break
        return frames
    cap = cv2.VideoCapture(path(item))
    n = 0
    while len(frames) < limit:
        ok, frame = cap.read()
        if not ok:
            break
        if n % every == 0:
            frames.append(frame)
        n += 1
    cap.release()
    return frames


@pytest.fixture(scope="module")
def clip():
    from similarity import ImageSimilarity
    sim = ImageSimilarity(db=mongomock.MongoClient()["media_test"])
    if not sim.available:
        pytest.skip("CLIP could not be loaded")
    return sim


@pytest.fixture(scope="module")
def jpgs(tmp_path_factory):
    """Every test image re-saved as a plain JPEG (the formats depots upload)."""
    out = tmp_path_factory.mktemp("media")
    paths = {}
    for item in IMAGES:
        p = str(out / f"{item['id']}.jpg")
        cv2.imwrite(p, read_image(item))
        paths[item["id"]] = p
    return paths


# ── the files themselves ──────────────────────────────────────────────────────
def test_every_file_is_attributed_and_freely_licensed():
    attribution = open(os.path.join(MEDIA, "ATTRIBUTION.md"), encoding="utf8").read()
    free = ("CC0", "CC BY ", "CC BY-SA ")
    for item in ITEMS:
        assert item["license"].startswith(free) or item["license"] == "CC0", item
        assert item["id"] in attribution and item["url"].startswith("https://upload.wikimedia.org/")


def test_every_image_decodes():
    for item in IMAGES:
        img = read_image(item)
        assert img is not None and img.shape[0] >= 100 and img.shape[1] >= 100, item["id"]


@pytest.mark.skipif(not VIDEOS, reason="no clips downloaded")
def test_every_clip_yields_frames():
    for item in VIDEOS:
        assert len(video_frames(item, every=3, limit=20)) >= 5, item["id"]


# ── the camera-scan frame gate on real footage ─────────────────────────────────
@pytest.mark.skipif(not VIDEOS, reason="no clips downloaded")
def test_frame_gate_keeps_new_views_and_drops_repeats():
    """Replays each clip as if a depot phone were filming the item: the gate keeps a
    handful of distinct, sharp views and rejects the near-identical in-between frames."""
    from item_intake import vision
    from item_intake.config import IntakeConfig
    cfg = IntakeConfig()
    for item in VIDEOS:
        frames = video_frames(item, every=2, limit=90)
        kept = []
        for f in frames:
            f = vision.resize_max(f, 960)
            if vision.sharpness(f) < cfg.min_sharpness:
                continue
            h = vision.phash(f)
            if any(vision.hamming(h, k) < cfg.min_view_distance for k in kept):
                continue
            kept.append(h)
        assert 2 <= len(kept) < len(frames), (item["id"], len(kept), len(frames))


# ── colours read by OpenCV ─────────────────────────────────────────────────────
FAMILY = {"navy blue": {"blue", "black"}, "blue": {"blue", "grey", "silver", "white"},
          "grey/brown": {"grey", "brown", "black", "silver"}, "red": {"red", "pink", "brown", "orange"},
          "black": {"black", "grey", "brown"}, "yellow": {"yellow", "gold", "orange"},
          "brown": {"brown", "orange", "gold", "black"}, "grey": {"grey", "silver", "black", "white"},
          "silver": {"silver", "grey", "white"}, "green": {"green", "grey"}, "teal": {"blue", "green", "grey"}}


def test_dominant_colours_of_real_items():
    from item_intake import vision
    checked = hits = 0
    for item in IMAGES:
        family = FAMILY.get((item.get("colour") or "").split("/")[0].strip()) or FAMILY.get(item.get("colour") or "")
        if not family:
            continue
        img = vision.resize_max(read_image(item), 640)
        names = [name for name, share, _lab in vision.dominant_colours(img, vision.item_mask(img))[:3]]
        checked += 1
        hits += bool(family & set(names))
    assert checked >= 20 and hits / checked >= 0.7, (hits, checked)


# ── CLIP on real photos ────────────────────────────────────────────────────────
def _groups():
    groups = defaultdict(list)
    for item in IMAGES:
        if item.get("same_object_group"):
            groups[item["same_object_group"]].append(item)
    return {g: v for g, v in groups.items() if len(v) >= 2}


def test_same_object_photos_find_each_other(clip, jpgs):
    """For each photo of a pictured object, its other photo should be the closest among
    every other photo of the same kind of item (instance retrieval, R@1)."""
    groups = _groups()
    assert len(groups) >= 6
    by_cat = defaultdict(list)
    for item in IMAGES:
        by_cat[item["app_category"]].append(item)
    hits = total = 0
    for members in groups.values():
        for query in members:
            pool = [i for i in by_cat[query["app_category"]] if i["id"] != query["id"]]
            best = max(pool, key=lambda i: clip.similarity(jpgs[query["id"]], jpgs[i["id"]]))
            total += 1
            hits += best.get("same_object_group") == query["same_object_group"]
    assert hits / total >= 0.75, (hits, total)


def test_clip_null_distribution_matches_the_matcher_calibration(clip, jpgs):
    """Different objects of the same kind: the matcher's IMAGE_NULL should describe them."""
    from matching import IMAGE_NULL
    sims = []
    for i, a in enumerate(IMAGES):
        for b in IMAGES[i + 1:]:
            if a["app_category"] == b["app_category"] and (
                    not a.get("same_object_group") or a.get("same_object_group") != b.get("same_object_group")):
                sims.append(clip.similarity(jpgs[a["id"]], jpgs[b["id"]]))
    mu, sd = mean(sims), pstdev(sims)
    print(f"\nCLIP photo-photo, different objects of one kind: mean {mu:.3f} sd {sd:.3f} (n={len(sims)})")
    assert abs(mu - IMAGE_NULL[0]) <= 0.12 and 0.03 <= sd <= 0.15


def test_descriptions_pick_the_right_photo(clip, jpgs):
    """Cross-modal: a passenger's words vs the depot's photos of every item of that kind."""
    by_cat = defaultdict(list)
    for item in IMAGES:
        by_cat[item["app_category"]].append(item)
    hits = total = 0
    for item in IMAGES:
        if not item.get("colour") or len(by_cat[item["app_category"]]) < 3:
            continue
        noun = {"bag": item["category"], "wallet": item["category"]}.get(item["app_category"], item["category"])
        text = f"{item['colour']} {item.get('brand') or ''} {noun}".replace("  ", " ").strip()
        ranked = sorted(by_cat[item["app_category"]], key=lambda i: -clip.text_to_images(text, [jpgs[i["id"]]]))
        top3 = [i["id"] for i in ranked[:3]]
        same = {i["id"] for i in IMAGES if i.get("same_object_group") and
                i.get("same_object_group") == item.get("same_object_group")} | {item["id"]}
        total += 1
        hits += bool(same & set(top3))
    assert hits / total >= 0.7, (hits, total)


def test_zero_shot_category_and_the_question_gate(clip, jpgs):
    """The camera scan's CLIP category on real photos; the gate only lets confident ones through."""
    from item_intake.analysis import CATEGORY_PROMPTS
    from item_intake.questions import question_category
    labels = list(CATEGORY_PROMPTS.values())
    correct = gated_wrong = sure = 0
    for item in IMAGES:
        res = clip.zero_shot([jpgs[item["id"]]], labels)
        ranked = sorted(res.items(), key=lambda kv: -kv[1])
        cat = next(k for k, v in CATEGORY_PROMPTS.items() if v == ranked[0][0])
        facts = {"category": cat, "categoryConfidence": ranked[0][1], "categoryMargin": ranked[0][1] - ranked[1][1]}
        ok = cat == item["app_category"]
        correct += ok
        if question_category(facts) != "other":
            sure += 1
            gated_wrong += not ok
    n = len(IMAGES)
    print(f"\nzero-shot category accuracy {correct}/{n}; confident {sure}, of which wrong {gated_wrong}")
    assert correct / n >= 0.7
    assert gated_wrong <= max(1, sure // 10)          # the gate lets almost no wrong category through


def test_matcher_ranks_the_owners_item_first_from_photos(clip, jpgs):
    """A passenger with a photo of their item vs every logged item of that kind (photos only,
    the same vague words for all): the v2 matcher must rank the pictured object first."""
    from matching import Matcher

    class Words:
        def similarity(self, a, b):
            return 0.3
    m = Matcher(Words(), clip, image_root="")
    groups = _groups()
    hits = total = 0
    for members in groups.values():
        query, target = members[0], members[1]
        cat = query["app_category"]
        cands = [{"found_id": i["id"], "notes": cat, "images": [jpgs[i["id"]]], "date": "2026-11-01"}
                 for i in IMAGES if i["app_category"] == cat and i["id"] != query["id"]]
        if len(cands) < 3:
            continue
        lost = {"request_id": "r", "description": cat, "image_path": jpgs[query["id"]], "date": "2026-11-01"}
        block = m.score_block(lost, cands, query_is_found=False)
        total += 1
        hits += block[0][0]["found_id"] == target["id"]
    assert total >= 5 and hits / total >= 0.7, (hits, total)
