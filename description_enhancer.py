"""
description_enhancer.py — a cleaner copy of a description for the text matcher.

The LLM (Groq, GROQ_REASONING_MODEL) removes filler, expands abbreviations
("blk", "w/", "bp") and orders the facts (item, colour, brand, contents, marks)
so SBERT compares facts rather than phrasing. The original is never replaced:
the cleaned copy is stored beside it (descriptionEnhanced / notesEnhanced) and
used only as the SBERT input; all fact rules still read the original.

Safety gate (the reason this is allowed near the matcher): the cleaned text may
not contain any colour, brand, content, material or number that the original
did not. An LLM that "helpfully" adds "black" to "a bag" would create false
matches, so such output is thrown away. Long digit runs (phone numbers, IMEIs)
are masked before anything is sent to the API.
"""
from __future__ import annotations

import logging
import os
import re

from claims.normalize import COLOUR_SET, normalize_text, tokens

logger = logging.getLogger(__name__)
MAX_INPUT = 1000

SYSTEM_PROMPT = (
    "You clean up descriptions of lost or found items for a bus lost-and-found search engine. "
    "Rewrite the text as one or two short plain-English sentences that keep EVERY fact exactly: the item, "
    "colours, brand, model, contents, marks, stickers, names and numbers. Remove filler words and "
    "feelings, expand abbreviations (blk = black, w/ = with, bp = backpack), and put the item first, then "
    "colour and brand, then what was inside, then marks. Never add a fact that is not in the text, never "
    "guess a colour or brand, never translate names. Output only the rewritten description.")


def _mask_numbers(text: str) -> str:
    return re.sub(r"\d[\d\s-]{5,}\d", lambda m: "[number]" if len(re.sub(r"\D", "", m.group())) >= 6 else m.group(),
                  text)


# Abbreviations people type; the rewrite may spell them out without "adding" a fact.
ABBREVIATIONS = {"blk": "black", "bk": "black", "wht": "white", "wt": "white", "blu": "blue", "gry": "grey",
                 "brn": "brown", "rd": "red", "grn": "green", "ylw": "yellow", "pnk": "pink", "slvr": "silver",
                 "bp": "backpack", "w/": "with", "w": "with", "lap": "laptop", "mob": "mobile", "ph": "phone",
                 "at": "american tourister", "wc": "wildcraft", "sb": "skybags"}


def _expand(text: str) -> str:
    words = re.split(r"(\s+)", text)
    return "".join(ABBREVIATIONS.get(w.lower().strip(".,;:!?"), w) if w.strip() else w for w in words)


def _facts(text: str):
    from matching import Facts
    from vocabulary import MATERIALS
    f = Facts(text)
    toks = set(tokens(text))
    return {"colours": set(f.colours) | (toks & COLOUR_SET), "brands": set(f.brands), "contents": set(f.contents),
            "materials": set(normalize_text(text).split()) & set(MATERIALS),
            "numbers": set(re.findall(r"\d+", text))}


def safe(original: str, enhanced: str) -> bool:
    """The cleaned text adds no colour, brand, content, material or number."""
    if not isinstance(enhanced, str) or not enhanced.strip() or len(enhanced) > 2 * len(original) + 80:
        return False
    a, b = _facts(_expand(original)), _facts(enhanced)
    return all(b[k] <= a[k] for k in a)


def enhance(text: str, client=None):
    """A cleaned copy of `text`, or None (no key, an error, or an unsafe rewrite)."""
    if not isinstance(text, str) or len(text.strip()) < 3:
        return None
    key = os.environ.get("GROQ_API_KEY")
    if client is None and not key:
        return None
    model = (os.environ.get("GROQ_REASONING_MODEL") or "openai/gpt-oss-120b").strip()
    original = _mask_numbers(text.strip()[:MAX_INPUT])
    try:
        import groq
        client = client or groq.Groq(api_key=key, timeout=12)
        resp = client.chat.completions.create(
            model=model, temperature=0, max_tokens=800,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": original}],
            **({"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {}))
        out = (resp.choices[0].message.content or "").strip().strip('"')
    except Exception as e:
        logger.warning("Description clean-up via %s failed: %s", model, e)
        return None
    if not safe(original, out):
        logger.info("Description clean-up discarded: it added facts that were not in the original.")
        return None
    return out


__all__ = ["enhance", "safe"]
