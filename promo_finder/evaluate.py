"""Compare a run against a hand-labelled gold set.

    python -m promo_finder.evaluate --run runs/<run_id> --gold eval/gold.json

gold.json: [{"id": "G1", "segments": [{"episode": 1, "start": "00:11.30", "end": "00:45.00"}], "note": "..."}]

A predicted promo matches a gold promo when they share an episode and their
time overlap / union (IoU) on that episode is >= --iou (default 0.3).
"""
import argparse
import json
from pathlib import Path


def to_sec(ts: str) -> float:
    m, s = ts.split(":")
    return int(m) * 60 + float(s)


def iou(a: list, b: list) -> float:
    inter = union = 0.0
    eps = {s["episode"] for s in a} | {s["episode"] for s in b}
    for e in eps:
        A = [(s["s"], s["e"]) for s in a if s["episode"] == e]
        B = [(s["s"], s["e"]) for s in b if s["episode"] == e]
        i = sum(max(0, min(x[1], y[1]) - max(x[0], y[0])) for x in A for y in B)
        inter += i
        union += sum(x[1] - x[0] for x in A) + sum(y[1] - y[0] for y in B) - i
    return inter / union if union else 0.0


def norm(segs):
    return [{"episode": int(s["episode"]), "s": to_sec(s["start"]), "e": to_sec(s["end"])} for s in segs]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--iou", type=float, default=0.3)
    a = ap.parse_args()
    promos = json.loads((Path(a.run) / "promos.json").read_text())["promos"]
    gold = json.loads(Path(a.gold).read_text())
    gold_eps = {s["episode"] for g in gold for s in g["segments"]}
    # only judge predictions on episodes the labeller actually covered
    preds = [p for p in promos if p["segments"][0]["episode"] in gold_eps]

    matches = []
    for g in gold:
        gs = norm(g["segments"])
        best = max(((iou(norm(p["segments"]), gs), p) for p in preds), key=lambda t: t[0], default=(0, None))
        if best[1] and best[0] >= a.iou:
            p = best[1]
            matches.append({"gold": g["id"], "pred": p["id"], "rank": p["rank"], "iou": round(best[0], 2),
                            "start_err": round(norm(p["segments"])[0]["s"] - gs[0]["s"], 2),
                            "end_err": round(norm(p["segments"])[-1]["e"] - gs[-1]["e"], 2)})
        else:
            matches.append({"gold": g["id"], "pred": None})
    hit = [m for m in matches if m["pred"]]
    matched_pred = {m["pred"] for m in hit}
    top = sorted(preds, key=lambda p: p["rank"])[:10]
    rep = {
        "gold_promos": len(gold), "episodes_labelled": sorted(gold_eps), "predictions_on_those_eps": len(preds),
        "recall": round(len(hit) / len(gold), 3) if gold else None,
        "precision_top10_vs_gold": round(sum(p["id"] in matched_pred for p in top) / len(top), 3) if top else None,
        # stricter: the hook must land in the first 3s, so the start must be within 3s of the gold start
        "hook_accurate_recall": round(sum(abs(m["start_err"]) <= 3 for m in hit) / len(gold), 3) if gold else None,
        "recall_in_top10": round(sum(m["rank"] <= 10 for m in hit) / len(gold), 3) if gold else None,
        "recall_in_top20": round(sum(m["rank"] <= 20 for m in hit) / len(gold), 3) if gold else None,
        "mean_abs_start_err_sec": round(sum(abs(m["start_err"]) for m in hit) / len(hit), 2) if hit else None,
        "mean_abs_end_err_sec": round(sum(abs(m["end_err"]) for m in hit) / len(hit), 2) if hit else None,
        "matches": matches,
    }
    print(json.dumps(rep, indent=2))
    (Path(a.run) / "eval.json").write_text(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
