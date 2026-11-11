"""
similarity.py — AI Matching Engine
===================================
This module handles ALL similarity logic for the recovery system.
It is imported by app.py and keeps the main app clean and readable.

Components:
  1. TextSimilarity   — Sentence-BERT (fast, cached in RAM)
  2. ImageSimilarity  — CLIP (optional; cached in MongoDB): photo↔photo,
                        description↔photo, and zero-shot labels for intake
  3. OCRExtractor     — OCR.Space

How these raw similarities become a match decision is matching.py (v2,
evidence in bits). The old fixed-weight UnifiedScorer is kept only as the
benchmark baseline in tools/legacy_matcher_v1.py.
"""

import hashlib
import logging
import os
import requests
from typing import Optional

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 1. TEXT SIMILARITY (Sentence-BERT)
# ──────────────────────────────────────────────
class TextSimilarity:
    """
    Wraps SentenceTransformer for semantic text similarity.

    Embeddings are cached in a plain dict (RAM cache) keyed by the
    MD5 hash of the input text. This means repeated calls with the
    same description are nearly instant (no GPU/CPU re-inference).
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading SBERT model: {model_name}")
        self.model = SentenceTransformer(model_name)
        # In-memory cache: { text_hash: embedding_ndarray }
        self._cache: dict = {}
        logger.info("SBERT model loaded [OK]")

    def _hash(self, text: str) -> str:
        """Return MD5 hash of text (used as cache key)."""
        return hashlib.md5(text.lower().strip().encode()).hexdigest()

    def embed(self, text: str) -> np.ndarray:
        """
        Get embedding for a text string.
        Uses cache to avoid recomputing the same text twice.
        """
        key = self._hash(text)
        if key not in self._cache:
            self._cache[key] = self.model.encode([text])[0]
        return self._cache[key]

    def similarity(self, text_a: str, text_b: str) -> float:
        """
        Compute cosine similarity between two texts.
        Returns a float in [0.0, 1.0].
        """
        if not text_a or not text_b:
            return 0.0
        emb_a = self.embed(text_a)
        emb_b = self.embed(text_b)
        score = cosine_similarity([emb_a], [emb_b])[0][0]
        return float(score)


# ──────────────────────────────────────────────
# 2. IMAGE SIMILARITY (CLIP)
# ──────────────────────────────────────────────
class ImageSimilarity:
    """
    Wraps OpenAI CLIP for visual image similarity.

    Embeddings are stored in MongoDB (image_embeddings collection) so
    that re-starting the server does NOT require re-running CLIP.
    If CLIP is unavailable (not installed / import error), all image
    scores gracefully return 0.0 and text+route scoring still works.
    """

    def __init__(self, db=None):
        """
        db: pymongo database object (for embedding cache collection).
        """
        self.db = db
        self.model = None
        self.processor = None
        self.available = False  # Will be True only if CLIP loads OK

        try:
            from transformers import CLIPProcessor, CLIPModel
            import torch
            logger.info("Loading CLIP model (openai/clip-vit-base-patch32)...")
            self.model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
            self.processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
            self.model.eval()  # Inference mode (no gradient tracking needed)
            self.available = True
            logger.info("CLIP model loaded [OK]")
        except Exception as e:
            logger.warning(
                f"CLIP unavailable — image scoring disabled. "
                f"Reason: {e}. "
                f"Install 'transformers' and 'torch' to enable it."
            )

    def _get_cached_embedding(self, image_path: str) -> Optional[np.ndarray]:
        """Look up a previously computed CLIP embedding in MongoDB."""
        if self.db is None:
            return None
        record = self.db["image_embeddings"].find_one({"image_path": image_path})
        if record and "embedding" in record:
            return np.array(record["embedding"], dtype=np.float32)
        return None

    def _save_embedding(self, image_path: str, embedding: np.ndarray):
        """Save a CLIP embedding to MongoDB for future reuse."""
        if self.db is None:
            return
        from datetime import datetime
        self.db["image_embeddings"].update_one(
            {"image_path": image_path},
            {"$set": {
                "image_path": image_path,
                "embedding": embedding.tolist(),
                "created_at": datetime.utcnow().isoformat()
            }},
            upsert=True   # Insert if not exists, update if exists
        )

    def embed(self, image_path: str) -> Optional[np.ndarray]:
        """
        Compute (or retrieve from cache) a CLIP embedding for an image file.
        image_path: absolute path to the image file on disk.
        Returns: numpy array of shape (512,) or None if CLIP unavailable.
        """
        if not self.available:
            return None

        # 1. Check MongoDB cache first
        cached = self._get_cached_embedding(image_path)
        if cached is not None:
            return cached

        # 2. Not cached — compute with CLIP
        try:
            import torch
            from PIL import Image

            image = Image.open(image_path).convert("RGB")
            inputs = self.processor(images=image, return_tensors="pt")
            with torch.no_grad():
                features = self.model.get_image_features(**inputs)
                
                # Handle old transformers version where it returns BaseModelOutputWithPooling
                if not isinstance(features, torch.Tensor) and hasattr(features, "pooler_output"):
                    features = features.pooler_output
                    
                # Normalize to unit vector (cosine similarity works best this way)
                features = features / features.norm(dim=-1, keepdim=True)
            embedding = features[0].cpu().numpy()

            # 3. Save to MongoDB cache
            self._save_embedding(image_path, embedding)
            return embedding

        except Exception as e:
            logger.error(f"CLIP embedding failed for {image_path}: {e}")
            return None

    def similarity(self, path_a: str, path_b: str) -> float:
        """
        Compute cosine similarity between two image files.
        Returns float in [0.0, 1.0], or 0.0 if either image fails.
        """
        if not self.available or not path_a or not path_b:
            return 0.0
        emb_a = self.embed(path_a)
        emb_b = self.embed(path_b)
        if emb_a is None or emb_b is None:
            return 0.0
        score = cosine_similarity([emb_a], [emb_b])[0][0]
        return float(score)

    def text_to_images(self, text: str, image_paths) -> Optional[float]:
        """
        Cross-modal: CLIP cosine between a passenger's description and the best
        of an item's photos. Most passengers have no photo of what they lost,
        but depots photograph every item, so this lets the description be
        compared with what the item actually looks like. Raw cosines are small
        (about 0.15-0.35); the matcher calibrates them per block.
        """
        if not self.available or not text or not image_paths:
            return None
        feats = [e for e in (self.embed(p) for p in image_paths) if e is not None]
        if not feats:
            return None
        try:
            emb = self._text_cache.get(text) if hasattr(self, "_text_cache") else None
            if emb is None:
                import torch
                inputs = self.processor(text=[f"a photo of {text}"], return_tensors="pt", padding=True,
                                        truncation=True, max_length=77)
                with torch.no_grad():
                    out = self.model.get_text_features(**inputs)
                    if not isinstance(out, torch.Tensor) and hasattr(out, "pooler_output"):
                        out = out.pooler_output
                    out = out / out.norm(dim=-1, keepdim=True)
                emb = out[0].cpu().numpy()
                if not hasattr(self, "_text_cache"):
                    self._text_cache = {}
                if len(self._text_cache) > 2048:
                    self._text_cache.clear()
                self._text_cache[text] = emb
            return float(max(float(np.dot(emb, f)) for f in feats))
        except Exception as e:
            logger.error(f"CLIP text-to-image failed: {e}")
            return None

    def zero_shot(self, image_paths, labels, template: str = "a photo of {}") -> Optional[dict]:
        """
        Phase 2 smart intake: CLIP zero-shot classification. Averages the (cached)
        embeddings of several views of one item and compares them with a text
        prompt per label. Returns {label: probability} summing to 1, or None when
        CLIP is unavailable. Used for category, material, features and contents.
        """
        if not self.available or not image_paths or not labels:
            return None
        feats = [e for e in (self.embed(p) for p in image_paths) if e is not None]
        if not feats:
            return None
        try:
            import torch
            img = np.mean(np.stack(feats), axis=0)
            img = img / (np.linalg.norm(img) or 1.0)
            inputs = self.processor(text=[template.format(label) for label in labels],
                                    return_tensors="pt", padding=True)
            with torch.no_grad():
                text = self.model.get_text_features(**inputs)
                if not isinstance(text, torch.Tensor) and hasattr(text, "pooler_output"):
                    text = text.pooler_output
                text = text / text.norm(dim=-1, keepdim=True)
            logits = 100.0 * (text.cpu().numpy() @ img)
            probs = np.exp(logits - logits.max())
            probs = probs / probs.sum()
            return {label: float(p) for label, p in zip(labels, probs)}
        except Exception as e:
            logger.error(f"CLIP zero-shot failed: {e}")
            return None


# ──────────────────────────────────────────────
# 2.5 OCR EXTRACTOR (OCR.Space)
# ──────────────────────────────────────────────
class OCRExtractor:
    """
    Extracts text from images using the free OCR.Space API.
    """
    def __init__(self):
        self.api_key = os.environ.get("OCR_API_KEY")
        self.api_url = "https://api.ocr.space/parse/image"

        # Check if the placeholder is still there or if it's missing
        if not self.api_key or "YOUR_" in self.api_key:
            self.api_key = None
            logger.warning("OCR API key missing or invalid. OCR extraction disabled.")

    def extract_text(self, image_path: str) -> str:
        """
        Send image to OCR.Space and return the extracted text.
        """
        if not self.api_key or not os.path.exists(image_path):
            return ""

        try:
            with open(image_path, 'rb') as f:
                # We need to send the file as multipart/form-data
                payload = {
                    'apikey': self.api_key,
                    'language': 'eng',
                    'isOverlayRequired': False,
                    'scale': True
                }
                ext = os.path.splitext(image_path)[1].lower()
                mime_type = "image/png" if ext == ".png" else "image/jpeg"
                response = requests.post(
                    self.api_url,
                    files={'file': (os.path.basename(image_path), f, mime_type)},
                    data=payload,
                    timeout=20,   # never hang a request (or the intake job) on the OCR service
                )

                if response.status_code == 200:
                    result = response.json()
                    if result.get("IsErroredOnProcessing") == False:
                        # Combine text from all parsed results
                        parsed_text = " ".join([
                            r.get("ParsedText", "").replace('\r', '').replace('\n', ' ')
                            for r in result.get("ParsedResults", [])
                        ])
                        return parsed_text.strip().lower()
                    else:
                        logger.error(f"OCR API Error: {result.get('ErrorMessage')}")
        except Exception as e:
            logger.error(f"OCR Extraction failed for {image_path}: {e}")

        return ""
