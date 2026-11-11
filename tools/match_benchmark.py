"""
Matching benchmark: the v1 matcher vs the v2 matcher on the labelled cases in
tests/match_cases.py, replayed in arrival order exactly as the app sees them.

    python tools/match_benchmark.py            # real SBERT (all-MiniLM-L6-v2)
    python tools/match_benchmark.py --fake     # deterministic word-overlap model (what the tests use)
    python tools/match_benchmark.py --null     # also print the SBERT null distribution (for TEXT_NULL)

Every found item and lost report "arrives" in date order (on a given day the
depot logs items before passengers report in the evening). Each arrival is
scored against what has already arrived on the other side, as
compute_and_save_matches / compute_matches_for_lost do; the first routing
decision for a pair sticks, and only a system rejection can be upgraded,
mirroring MatchNotifyService.record_match.

Metrics
  auto ok / auto WRONG   pairs auto-notified that are / are not the passenger's item
  FANR                   wrong auto-notifications per lost report (target 0)
  surfaced               true pairs that reached the passenger or staff (auto or review)
  review junk            wrong pairs put in front of staff
  R@1, MRR               rank of the true item among a lost report's candidates
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import match_cases as cases                                    # noqa: E402
from legacy_matcher_v1 import score_v1                         # noqa: E402
from matching import Matcher, is_contested, route              # noqa: E402

AUTO_MIN, REVIEW_MIN, POSSIBLE_MIN = 0.85, 0.60, 0.30
RANK = {None: 0, "reject": 1, "review": 2, "auto": 3}


class WordOverlap:
    """Deterministic stand-in for SBERT: Jaccard overlap of folded tokens."""
    null_stats = (0.08, 0.08)

    def similarity(self, a, b):
        from claims.normalize import tokens
        wa, wb = set(tokens(a)), set(tokens(b))
        return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


class NoImages:
    available = False


def route_v1(score):
    f = score["final"]
    if f >= AUTO_MIN:
        return "auto"
    if f >= REVIEW_MIN:
        return "review"
    return "reject" if score["is_match"] else None


def route_v2(score):
    return route(score, AUTO_MIN, REVIEW_MIN)


def _arrivals():
    events = [(f["date"], 0, "found", f) for f in cases.FOUND] + [(lo["date"], 1, "lost", lo) for lo in cases.LOST]
    return sorted(events, key=lambda e: (e[0], e[1]))


def simulate(version, text_sim, image_sim=None):
    """{(lost_id, found_id): decision} after replaying every arrival."""
    image_sim = image_sim or NoImages()
    matcher = Matcher(text_sim, image_sim)
    arrived_found, arrived_lost, decisions = [], [], {}

    def record(lost, found, decision):
        key = (lost["request_id"], found["found_id"])
        old = decisions.get(key)
        if old is None or (old == "reject" and RANK[decision] > RANK[old]):
            if decision is not None:
                decisions[key] = decision

    for _date, _o, kind, doc in _arrivals():
        if kind == "found":
            arrived_found.append(doc)
            dates = cases.window(doc["date"], 7, forward=False)
            cands = [lo for lo in arrived_lost if lo["date"] in dates]
            if version == 1:
                for lo in cands:
                    record(lo, doc, route_v1(score_v1(doc, lo, text_sim)))
                continue

            def contested(top, found=doc, cands=cands, block=None):
                anchor = top["_cand"]
                fdates = cases.window(anchor["date"], 7, forward=True)
                rivals = [f for f in arrived_found if f is not found and f["date"] in fdates]
                return is_contested(matcher, anchor, found, rivals, anchor_is_found=False)
            block = matcher.score_block(doc, cands, query_is_found=True)
            _apply_contested(block, contested)
            for lo, s in block:
                record(lo, doc, route_v2(s))
        else:
            arrived_lost.append(doc)
            dates = cases.window(doc["date"], 7, forward=True)
            cands = [f for f in arrived_found if f["date"] in dates]
            if version == 1:
                for f in cands:
                    record(doc, f, route_v1(score_v1(f, doc, text_sim)))
                continue

            def contested(top, lost=doc):
                anchor = top["_cand"]
                ldates = cases.window(anchor["date"], 7, forward=False)
                rivals = [lo for lo in arrived_lost if lo is not lost and lo["date"] in ldates]
                return is_contested(matcher, anchor, lost, rivals, anchor_is_found=True)
            block = matcher.score_block(doc, cands, query_is_found=False)
            _apply_contested(block, contested)
            for f, s in block:
                record(doc, f, route_v2(s))
    return decisions


def _apply_contested(block, contested):
    """Re-run the mutual-best check for the top candidate (gate() already ran the rest)."""
    if block and block[0][1]["autoEligible"]:
        top = dict(block[0][1], _cand=block[0][0])
        if contested(top):
            block[0][1]["autoEligible"] = False
            block[0][1]["blockers"].append("this item is also a strong match for another report")


def rankings(version, text_sim, image_sim=None):
    """For each lost report with a true item: the rank of that item among its candidates."""
    matcher = Matcher(text_sim, image_sim or NoImages())
    ranks = {}
    for lo in cases.LOST:
        if not lo["truth"]:
            continue
        cands = cases.found_candidates(lo)
        if version == 1:
            order = sorted(cands, key=lambda f: -score_v1(f, lo, text_sim)["final"])
        else:
            order = [f for f, _s in matcher.score_block(lo, cands, query_is_found=False)]
        ids = [f["found_id"] for f in order]
        ranks[lo["request_id"]] = ids.index(lo["truth"]) + 1 if lo["truth"] in ids else None
    return ranks


def metrics(version, text_sim, image_sim=None):
    decisions = simulate(version, text_sim, image_sim)
    truth = {(lo["request_id"], lo["truth"]) for lo in cases.LOST if lo["truth"]}
    autos = {k for k, d in decisions.items() if d == "auto"}
    surfaced = {k for k, d in decisions.items() if d in ("auto", "review")}
    ranks = rankings(version, text_sim, image_sim)
    found_ranks = [r for r in ranks.values() if r]
    return {
        "auto_ok": len(autos & truth), "auto_wrong": len(autos - truth),
        "fanr": round(len(autos - truth) / len(cases.LOST), 3),
        "surfaced": f"{len(surfaced & truth)}/{len(truth)}",
        "review_junk": len({k for k, d in decisions.items() if d == "review"} - truth),
        "r_at_1": round(sum(r == 1 for r in found_ranks) / len(ranks), 3),
        "mrr": round(sum(1 / r for r in found_ranks) / len(ranks), 3),
        "wrong_autos": sorted(autos - truth), "missed": sorted(truth - surfaced),
    }


def null_distribution(text_sim):
    from statistics import mean, pstdev
    sims = [text_sim.similarity(lo["description"], f["notes"]) for lo in cases.LOST for f in cases.FOUND
            if f["found_id"] != lo["truth"] and f.get("notes")]
    true = [text_sim.similarity(lo["description"], next(f for f in cases.FOUND if f["found_id"] == lo["truth"])["notes"])
            for lo in cases.LOST if lo["truth"]]
    return {"null_mean": round(mean(sims), 3), "null_sd": round(pstdev(sims), 3),
            "true_mean": round(mean(true), 3), "true_min": round(min(true), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--null", action="store_true")
    args = ap.parse_args()
    if args.fake:
        text_sim = WordOverlap()
    else:
        from similarity import TextSimilarity
        text_sim = TextSimilarity()
    if args.null:
        print("text similarity null distribution:", null_distribution(text_sim))
    print(f"{len(cases.LOST)} lost reports, {len(cases.FOUND)} found items, "
          f"{sum(1 for lo in cases.LOST if lo['truth'])} true pairs")
    for version in (1, 2):
        m = metrics(version, text_sim)
        print(f"\nv{version}: auto ok {m['auto_ok']}  auto WRONG {m['auto_wrong']}  FANR {m['fanr']}  "
              f"surfaced {m['surfaced']}  review junk {m['review_junk']}  R@1 {m['r_at_1']}  MRR {m['mrr']}")
        if m["wrong_autos"]:
            print("   wrong auto-notifications:", m["wrong_autos"])
        if m["missed"]:
            print("   true pairs never surfaced:", m["missed"])


if __name__ == "__main__":
    main()
