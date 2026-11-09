"""
Description writers for the smart-intake scan (the "provider switch").

  basic     — a sentence assembled from the extracted attributes; instant, always works.
  florence  — Microsoft Florence-2-base (MIT licence, ~0.23B parameters) run locally
              on CPU through transformers. Offline after the first download.
              Measured on the project laptop: ~100 s first load, ~60 s per caption,
              so it runs inside the background job with a progress bar.

A cloud provider (e.g. Claude Haiku over the API) can be added later as another
class with the same `caption(image_path, facts)` method; nothing else changes.
"""
import logging
import threading

logger = logging.getLogger(__name__)


def _join(items):
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


class BasicCaptioner:
    name = "basic"

    def caption(self, image_path, facts: dict) -> str:
        colours = [c["name"] for c in facts.get("colours", [])[:2]]
        noun = facts.get("noun") or "item"
        material = facts.get("material")
        parts = [_join(colours), material, noun]
        text = "A " + " ".join(p for p in parts if p)
        features = [f["name"] for f in facts.get("features", [])]
        if features:
            text += f" with {_join(features)}"
        return text + "."


class FlorenceCaptioner:
    name = "florence"

    def __init__(self, model_id: str):
        self.model_id = model_id
        self._model = None
        self._processor = None
        self._lock = threading.Lock()
        self._failed = False
        self.fallback = BasicCaptioner()
        self.last_provider = self.name

    def _load(self):
        if self._model is not None or self._failed:
            return
        try:
            from transformers import AutoProcessor, Florence2ForConditionalGeneration
            logger.info("Loading %s for item descriptions (first use only)...", self.model_id)
            self._processor = AutoProcessor.from_pretrained(self.model_id)
            self._model = Florence2ForConditionalGeneration.from_pretrained(self.model_id)
            self._model.eval()
            logger.info("Florence-2 loaded.")
        except Exception as e:
            self._failed = True
            logger.warning("Florence-2 unavailable (%s); using the basic description writer.", e)

    def caption(self, image_path, facts: dict) -> str:
        with self._lock:                      # one model instance; CPU inference one at a time
            self._load()
            self.last_provider = "basic"
            if self._model is None:
                return self.fallback.caption(image_path, facts)
            try:
                import torch
                from PIL import Image
                image = Image.open(image_path).convert("RGB")
                inputs = self._processor(text="<CAPTION>", images=image, return_tensors="pt")
                with torch.no_grad():
                    out = self._model.generate(input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"],
                                               max_new_tokens=40, num_beams=1, do_sample=False)
                raw = self._processor.batch_decode(out, skip_special_tokens=False)[0]
                parsed = self._processor.post_process_generation(raw, task="<CAPTION>", image_size=image.size)
                text = str(parsed.get("<CAPTION>", "")).strip()
                if text:
                    self.last_provider = self.name
                    return text[0].upper() + text[1:] + ("" if text.endswith(".") else ".")
            except Exception as e:
                logger.warning("Florence-2 caption failed (%s); using the basic description.", e)
            return self.fallback.caption(image_path, facts)


def build_captioner(config):
    if config.vision_provider == "florence":
        return FlorenceCaptioner(config.florence_model)
    return BasicCaptioner()
