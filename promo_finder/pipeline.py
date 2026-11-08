"""End-to-end promo finding pipeline.

parse (code) -> rule signals (code) -> candidates (LLM, or rules baseline)
-> validate & snap to real blocks (code) -> score (LLM scores + rules)
-> dedupe + diversity rank (code) -> editor's posting plan (LLM) -> outputs
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

from .llm import LLM, LLMError
from .parser import Block, Episode, load_show, sec_to_ts
from .signals import annotate

MIN_LEN, MAX_LEN, TOL = 20.0, 60.0, 1.5
HOOK_WINDOW = 3.0
TROPES = ["betrayal", "humiliation", "slap/violence", "revenge", "secret identity",
          "romance/tension", "family cruelty", "rescue", "jealousy", "other"]

Progress = Callable[[str, float, str], None]


def _noop(step, frac, msg):
    pass


# ---------------------------------------------------------------- LLM view
def short_ts(x: float) -> str:
    m, s = divmod(x, 60)
    return f"{int(m)}:{s:04.1f}"


def llm_view(ep: Episode) -> str:
    out = []
    for b in ep.blocks:
        if b.kind == "CAMERA":
            continue
        if b.kind == "DIALOGUE":
            emo = f"({b.emotion})" if b.emotion else ""
            out.append(f"{b.idx}|{short_ts(b.start)}|{b.speaker}{emo}: {b.text}")
        else:
            out.append(f"{b.idx}|{short_ts(b.start)}|<{b.kind}> {b.text}")
    out.append(f"END|{short_ts(ep.duration)}|<episode ends>")
    return "\n".join(out)


SYSTEM_PROMPT = """You are a senior promo editor for vertical microdrama apps (ReelShort / DramaBox style). You pick 20-60 second promo clips out of screenplays; they are posted on TikTok, Reels and Shorts to pull in NEW viewers who have never seen the show.

A great promo:
- HOOK in the first 3 seconds: a slap, a reveal, a shocking or cruel line, a humiliation, a threat. Never a slow establishing beat.
- Ends on a CLIFFHANGER: cut at an unresolved moment (a question left hanging, a threat, a reveal before the reaction, a decision not yet made). Never cut after the resolution.
- STANDALONE: a cold viewer understands who is wronged and what is at stake without prior episodes.
- Does not spoil the show's big late payoffs.

Input: episodes separated by "=== EPISODE n ===". Each line is `idx|start_time|SPEAKER(emotion): line` or `idx|start_time|<ACTION or SOUND FX> description`. A line lasts until the next line starts.

Rules:
- Refer to moments ONLY by the idx numbers shown. Never invent an idx. A segment is {"ep": n, "from": idx, "to": idx} (inclusive).
- The first segment's `from` line IS the hook: it must be the attention-grabbing line/action itself, not setup before it.
- Total duration of all segments must be 20-60 seconds. Compute it from the start times.
- Prefer ONE continuous segment. Use 2-3 segments only when joining makes a much stronger promo (e.g. a hook moment + a later cliffhanger, possibly from another episode in this batch). Segments play in the order listed.
- Propose 1-3 promos per episode, only ones you would really post. Quality over quantity.
- Be a strict scorer (1-5). 5 = would stop almost anyone scrolling. Most promos are 3-4. spoiler_risk: 1 = safe, 5 = gives away a major twist.

Return ONLY JSON:
{"promos": [{"title": "short internal name", "segments": [{"ep": 1, "from": 7, "to": 31}], "hook": "what happens in the first 3s (quote the line if dialogue)", "cliffhanger": "the unresolved final moment", "why": "one line on why it works for a cold viewer", "trope": "one of: %s", "scores": {"hook": 4, "cliffhanger": 4, "standalone": 3, "spoiler_risk": 1}}]}""" % ", ".join(TROPES)


EDITOR_PROMPT = """You are the head of growth marketing for a microdrama app. You get a ranked shortlist of promo clips (already validated for length, hook and cliffhanger) for one show. Decide the posting plan for launch week.
Consider: which promos best explain the premise to a cold audience, early episodes funnel viewers into episode 1, variety of tropes, and spoiler risk.
Return ONLY JSON: {"post_order": [{"id": "P01", "reason": "one line: why post it at this position"}], "launch_note": "2-3 sentences on the overall strategy", "concerns": [{"id": "P05", "issue": "one line"}]}
Include at most 10 ids in post_order."""


# ---------------------------------------------------------------- candidates
def llm_candidates(eps: list[Episode], llm: LLM, show: str, batch: int, progress: Progress,
                   raw_log: list) -> list[dict]:
    premise = eps[0].logline if eps else ""
    batches = [eps[i:i + batch] for i in range(0, len(eps), batch)]
    done = [0]
    lock = threading.Lock()

    def work(group):
        label = f"episodes {group[0].ep}-{group[-1].ep}"
        parts = [f"SHOW: {show}", f"PREMISE (from episode 1): {premise}", ""]
        for e in group:
            parts += [f"=== EPISODE {e.ep} ===", f"Logline: {e.logline}", llm_view(e), ""]
        log = lambda m: progress("candidates", done[0] / len(batches), f"[{label}] {m}")
        try:
            res = llm.chat_json(SYSTEM_PROMPT, "\n".join(parts), max_tokens=4000, tag=label, log=log)
            out = (label, res, None)
        except LLMError as e:
            out = (label, None, e)
        with lock:
            done[0] += 1
            progress("candidates", done[0] / len(batches),
                     f"LLM read {label} ({done[0]}/{len(batches)})" + (f" - FAILED: {out[2]}" if out[2] else ""))
        return out

    progress("candidates", 0.0, f"{len(batches)} LLM calls, {llm.concurrency} in parallel")
    with ThreadPoolExecutor(max_workers=llm.concurrency) as pool:
        results = list(pool.map(work, batches))  # map keeps batch order -> deterministic output

    cands = []
    for label, res, err in results:
        if err:
            raw_log.append({"batch": label, "error": str(err)})
            continue
        raw_log.append({"batch": label, "response": res})
        for p in (res.get("promos", []) if isinstance(res, dict) else []):
            p["source"] = "llm"
            p["batch"] = label
            cands.append(p)
    fatal = [e for _, _, e in results if e and ("Daily token limit" in str(e) or "rejected" in str(e)
                                                  or "No credentials" in str(e))]
    if fatal and not cands:
        raise fatal[0]
    progress("candidates", 1.0, f"{len(cands)} raw candidates from LLM")
    return cands


def rule_candidates(eps: list[Episode], sig: dict) -> list[dict]:
    """Baseline with no LLM: start on a strong hook block, end on the best cliff block 20-60s later."""
    cands = []
    for ep in eps:
        nb = [b for b in ep.blocks if b.kind != "CAMERA"]
        hooks = sorted(nb, key=lambda b: -sig[b.id]["hook"])
        used: list[float] = []
        for h in hooks:
            if sig[h.id]["hook"] < 0.3 or len(used) >= 2:
                break
            if any(abs(h.start - u) < 25 for u in used):
                continue
            ends = [b for b in nb if MIN_LEN <= b.end - h.start <= MAX_LEN]
            if not ends:
                continue
            e = max(ends, key=lambda b: (sig[b.id]["cliff"], b.end))
            used.append(h.start)
            cands.append({
                "title": f"Ep{ep.ep} rule pick @{short_ts(h.start)}", "source": "rules",
                "segments": [{"ep": ep.ep, "from": h.idx, "to": e.idx}],
                "hook": f"{h.speaker + ': ' if h.speaker else ''}{h.text}"[:160],
                "cliffhanger": f"{e.speaker + ': ' if e.speaker else ''}{e.text}"[:160],
                "why": "Strongest keyword/emotion hook in the episode, cut on the highest cliffhanger signal.",
                "trope": "other", "scores": {},
            })
    return cands


# ---------------------------------------------------------------- validation
def _snap_start(ep: Episode, b: Block) -> float:
    """Start on the shot (CAMERA block) that contains the hook, if it begins just before it."""
    best = b.start
    for c in ep.blocks:
        if c.kind == "CAMERA" and c.start <= b.start < c.end and b.start - c.start <= 1.0:
            best = min(best, c.start)
    return best


def _snap_end(ep: Episode, b: Block) -> float:
    """End at the end of the shot that is running when the last line finishes (avoid mid-shot cuts)."""
    best = b.end
    for c in ep.blocks:
        if c.kind == "CAMERA" and c.start < b.end <= c.end and c.end - b.end <= 1.5:
            best = max(best, c.end)
    return best


def build_promo(raw: dict, by_ep: dict[int, Episode], sig: dict) -> tuple[dict | None, str]:
    segs_in = raw.get("segments") or []
    if not isinstance(segs_in, list) or not segs_in:
        return None, "no segments"
    flags, segs = [], []
    for s in segs_in[:4]:
        try:
            epn, a, z = int(s["ep"]), int(s["from"]), int(s["to"])
        except (KeyError, TypeError, ValueError):
            return None, f"malformed segment {s}"
        ep = by_ep.get(epn)
        if not ep:
            return None, f"unknown episode {epn}"
        if not (0 <= a < len(ep.blocks) and 0 <= z < len(ep.blocks)):
            return None, f"block idx out of range in ep {epn} ({a}-{z})"
        if z < a:
            a, z = z, a
            flags.append("swapped_from_to")
        segs.append({"ep": ep, "a": a, "z": z})

    # dead-air trim: the hook must land inside the first 3s
    first = segs[0]
    ep0 = first["ep"]
    nb = [b for b in ep0.blocks[first["a"]:first["z"] + 1] if b.kind != "CAMERA"]
    if not nb:
        return None, "first segment has no dialogue/action"
    start0 = _snap_start(ep0, ep0.blocks[first["a"]])
    if nb[0].start - start0 > HOOK_WINDOW:
        first["a"] = nb[0].idx
        flags.append("trimmed_dead_air")

    def seg_bounds(s):
        ep = s["ep"]
        blocks = ep.blocks[s["a"]:s["z"] + 1]
        last = max((b for b in blocks if b.kind != "CAMERA"), key=lambda b: b.end, default=blocks[-1])
        return _snap_start(ep, ep.blocks[s["a"]]), _snap_end(ep, last)

    total = sum(e - st for st, e in map(seg_bounds, segs))

    # too short: extend the last segment forward within the same scene (flagged: cliffhanger may move)
    if total < MIN_LEN - TOL:
        last = segs[-1]
        ep = last["ep"]
        scene = ep.blocks[last["z"]].scene
        z = last["z"]
        while total < MIN_LEN and z + 1 < len(ep.blocks) and ep.blocks[z + 1].scene == scene:
            z += 1
            last["z"] = z
            total = sum(e - st for st, e in map(seg_bounds, segs))
        if total < MIN_LEN - TOL:
            return None, f"too short ({total:.1f}s) even after extending"
        flags.append("extended_to_min_length")
    if total > MAX_LEN + TOL:
        return None, f"too long ({total:.1f}s)"

    out_segs, script = [], []
    for s in segs:
        ep = s["ep"]
        st, en = seg_bounds(s)
        blocks = [b for b in ep.blocks if b.kind != "CAMERA" and st - 0.01 <= b.start < en]
        out_segs.append({"episode": ep.ep, "start": sec_to_ts(st), "end": sec_to_ts(en),
                         "start_sec": round(st, 2), "end_sec": round(en, 2),
                         "duration": round(en - st, 2),
                         "from_block": ep.blocks[s["a"]].id, "to_block": ep.blocks[s["z"]].id})
        script.append([{"t": sec_to_ts(b.start), "kind": b.kind, "speaker": b.speaker,
                        "emotion": b.emotion, "text": b.text} for b in blocks])

    # rule checks on the actual cut
    seg0 = out_segs[0]
    ep0 = by_ep[seg0["episode"]]
    hook_blocks = [b for b in ep0.blocks if b.kind != "CAMERA"
                   and seg0["start_sec"] - 0.01 <= b.start < seg0["start_sec"] + HOOK_WINDOW]
    rule_hook = max((sig[b.id]["hook"] for b in hook_blocks), default=0.0)
    segl = out_segs[-1]
    epl = by_ep[segl["episode"]]
    tail = [b for b in epl.blocks if segl["end_sec"] - 5 <= b.end <= segl["end_sec"] + 0.01]
    rule_cliff = max((sig[b.id]["cliff"] for b in tail), default=0.0)
    if not hook_blocks:
        flags.append("no_line_in_first_3s")
    if len(out_segs) > 1:
        flags.append("multi_segment")
    if len({s["episode"] for s in out_segs}) > 1:
        flags.append("cross_episode")
    resolved_tail = [b for b in tail if b.kind == "DIALOGUE"]
    if resolved_tail and resolved_tail[-1].text.rstrip().endswith(".") and rule_cliff < 0.3:
        flags.append("weak_ending_signal")

    scores = raw.get("scores") or {}

    def sc(k, d):
        try:
            return max(1, min(5, int(round(float(scores.get(k, d))))))
        except (TypeError, ValueError):
            return d

    promo = {
        "title": str(raw.get("title", ""))[:80],
        "source": raw.get("source", "llm"),
        "segments": out_segs,
        "total_duration": round(total, 2),
        "hook": raw.get("hook", ""), "cliffhanger": raw.get("cliffhanger", ""),
        "why": raw.get("why", ""),
        "trope": raw.get("trope", "other") if raw.get("trope") in TROPES else "other",
        "llm_scores": {"hook": sc("hook", 0), "cliffhanger": sc("cliffhanger", 0),
                       "standalone": sc("standalone", 0), "spoiler_risk": sc("spoiler_risk", 1)} if scores else {},
        "rule_scores": {"hook": round(rule_hook, 2), "cliffhanger": round(rule_cliff, 2)},
        "flags": flags,
        "script": script,
    }
    return promo, ""


# ---------------------------------------------------------------- scoring / ranking
def score(p: dict, n_eps: int) -> float:
    r = p["rule_scores"]
    first_ep = p["segments"][0]["episode"]
    funnel = 4 if first_ep <= max(3, n_eps // 10) else (2 if first_ep <= max(8, n_eps // 4) else 0)
    if p["llm_scores"]:
        l = p["llm_scores"]
        s = 100 * (0.28 * l["hook"] / 5 + 0.28 * l["cliffhanger"] / 5 + 0.20 * l["standalone"] / 5
                   + 0.12 * r["hook"] + 0.12 * r["cliffhanger"])
        s -= 4 * (l["spoiler_risk"] - 1)
    else:
        s = 100 * (0.5 * r["hook"] + 0.5 * r["cliffhanger"])
    s += funnel
    if "cross_episode" in p["flags"]:
        s -= 3  # stitched promos are harder to make coherent in the edit
    if "extended_to_min_length" in p["flags"]:
        s -= 3
    if "no_line_in_first_3s" in p["flags"]:
        s -= 8
    return round(max(0.0, min(100.0, s)), 1)


def _overlap(a: dict, b: dict) -> float:
    inter = 0.0
    for x in a["segments"]:
        for y in b["segments"]:
            if x["episode"] == y["episode"]:
                inter += max(0.0, min(x["end_sec"], y["end_sec"]) - max(x["start_sec"], y["start_sec"]))
    return inter / max(1e-6, min(a["total_duration"], b["total_duration"]))


def dedupe_and_rank(promos: list[dict]) -> tuple[list[dict], list[dict]]:
    promos = sorted(promos, key=lambda p: -p["score"])
    kept, dropped = [], []
    for p in promos:
        dup = next((k for k in kept if _overlap(p, k) > 0.5), None)
        if dup:
            dropped.append({"title": p["title"], "reason": f"overlaps >50% with higher-scored '{dup['title']}'"})
        else:
            kept.append(p)
    # greedy diversity re-rank: repeated tropes / episodes get a soft penalty
    ranked, pool = [], kept[:]
    trope_n: dict[str, int] = {}
    ep_n: dict[int, int] = {}
    while pool:
        def adj(p):
            e = p["segments"][0]["episode"]
            return p["score"] * (0.93 ** trope_n.get(p["trope"], 0)) * (0.9 ** ep_n.get(e, 0))
        best = max(pool, key=adj)
        pool.remove(best)
        best["rank_score"] = round(adj(best), 1)
        ranked.append(best)
        trope_n[best["trope"]] = trope_n.get(best["trope"], 0) + 1
        e = best["segments"][0]["episode"]
        ep_n[e] = ep_n.get(e, 0) + 1
    for i, p in enumerate(ranked):
        p["rank"] = i + 1
        p["id"] = f"P{i + 1:02d}"
    return ranked, dropped


def editor_plan(ranked: list[dict], llm: LLM, show: str) -> dict:
    short = [{"id": p["id"], "title": p["title"], "episodes": sorted({s["episode"] for s in p["segments"]}),
              "duration": p["total_duration"], "trope": p["trope"], "hook": p["hook"],
              "cliffhanger": p["cliffhanger"], "scores": p["llm_scores"], "flags": p["flags"]}
             for p in ranked[:15]]
    user = f"SHOW: {show}\nPREMISE: see promos.\nSHORTLIST (ranked by score):\n{json.dumps(short, indent=0)}"
    return llm.chat_json(EDITOR_PROMPT, user, max_tokens=2500, tag="editor")


# ---------------------------------------------------------------- quality report
def quality(eps, raw_cands, ranked, rejected, dropped) -> dict:
    n_raw = len(raw_cands)
    disagree = []
    for p in ranked:
        l = p["llm_scores"]
        if not l:
            continue
        for k in ("hook", "cliffhanger"):
            gap = l[k] / 5 - p["rule_scores"][k]
            if abs(gap) >= 0.6:
                disagree.append({"id": p["id"], "dimension": k, "llm": l[k], "rules": p["rule_scores"][k],
                                 "note": "LLM rates it high but no textual signal (check the video)" if gap > 0
                                 else "Rules see a strong signal the LLM rated low"})
    covered = {s["episode"] for p in ranked for s in p["segments"]}
    flag_counts: dict[str, int] = {}
    for p in ranked:
        for f in p["flags"]:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    durs = [p["total_duration"] for p in ranked]
    return {
        "episodes": len(eps), "raw_candidates": n_raw, "valid": len(ranked) + len(dropped),
        "rejected_invalid": len(rejected), "dropped_duplicates": len(dropped), "final": len(ranked),
        "validity_rate": round((len(ranked) + len(dropped)) / n_raw, 3) if n_raw else None,
        "episodes_with_a_promo": len(covered),
        "episodes_without_promo": sorted({e.ep for e in eps} - covered),
        "duration_sec": {"min": min(durs, default=0), "max": max(durs, default=0),
                         "mean": round(sum(durs) / len(durs), 1) if durs else 0},
        "flag_counts": flag_counts,
        "rules_vs_llm_disagreements": disagree,
        "rejection_reasons": rejected,
    }


# ---------------------------------------------------------------- writers
def write_outputs(out: Path, meta: dict, ranked: list[dict], q: dict, plan: dict, raw_log: list,
                  raw_cands: list, dropped: list):
    out.mkdir(parents=True, exist_ok=True)
    (out / "promos.json").write_text(json.dumps({"meta": meta, "editor_plan": plan, "promos": ranked}, indent=2))
    (out / "quality.json").write_text(json.dumps(q, indent=2))
    (out / "candidates_raw.json").write_text(json.dumps(raw_cands, indent=2))
    (out / "llm_responses.json").write_text(json.dumps(raw_log, indent=2))
    (out / "dropped_duplicates.json").write_text(json.dumps(dropped, indent=2))
    with (out / "promos.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "id", "score", "title", "segments", "total_sec", "hook", "cliffhanger", "why",
                    "trope", "llm_hook", "llm_cliff", "llm_standalone", "spoiler_risk", "rule_hook",
                    "rule_cliff", "flags"])
        for p in ranked:
            l = p["llm_scores"] or {}
            w.writerow([p["rank"], p["id"], p["rank_score"], p["title"],
                        " + ".join(f"Ep{s['episode']} {s['start']}-{s['end']}" for s in p["segments"]),
                        p["total_duration"], p["hook"], p["cliffhanger"], p["why"], p["trope"],
                        l.get("hook", ""), l.get("cliffhanger", ""), l.get("standalone", ""),
                        l.get("spoiler_risk", ""), p["rule_scores"]["hook"], p["rule_scores"]["cliffhanger"],
                        ";".join(p["flags"])])
    md = [f"# Promos — {meta['show']}", "",
          f"Run `{meta['run_id']}` · mode **{meta['mode']}** · model `{meta.get('model', '-')}` · "
          f"{q['final']} promos from {q['episodes']} episodes · {meta['elapsed_sec']}s", ""]
    if plan:
        md += ["## Posting plan", "", plan.get("launch_note", ""), ""]
        for i, o in enumerate(plan.get("post_order", []), 1):
            md.append(f"{i}. **{o.get('id')}** — {o.get('reason', '')}")
        md.append("")
    md += ["## Ranked promos", ""]
    for p in ranked:
        segs = " + ".join(f"Ep {s['episode']} [{s['start']} → {s['end']}]" for s in p["segments"])
        md += [f"### {p['id']} · {p['title']} · score {p['rank_score']}",
               f"- **Cut:** {segs} ({p['total_duration']}s)",
               f"- **Hook:** {p['hook']}", f"- **Cliffhanger:** {p['cliffhanger']}",
               f"- **Why:** {p['why']}",
               f"- trope `{p['trope']}` · LLM {p['llm_scores'] or '-'} · rules {p['rule_scores']}"
               + (f" · flags: {', '.join(p['flags'])}" if p['flags'] else ""), ""]
    md += ["## Quality", "", "```json", json.dumps({k: v for k, v in q.items() if k != 'rejection_reasons'}, indent=1),
           "```"]
    (out / "report.md").write_text("\n".join(md))


# ---------------------------------------------------------------- entry
def run(data_dir: str | Path, out_root: str | Path = "runs", mode: str = "auto",
        progress: Progress = _noop, show_name: str | None = None, llm: LLM | None = None) -> Path:
    t0 = time.time()
    data_dir = Path(data_dir)
    show = show_name or data_dir.name.replace("-", " ").title()
    progress("parse", 0.0, "Parsing screenplays")
    eps = load_show(data_dir)
    if not eps:
        raise ValueError(f"No parseable episodes in {data_dir}")
    by_ep = {e.ep: e for e in eps}
    progress("signals", 0.0, f"Parsed {len(eps)} episodes, {sum(len(e.blocks) for e in eps)} blocks; scoring rule signals")
    sig = annotate(eps)

    llm = llm or LLM()
    if mode == "auto":
        mode = "llm" if llm.available else "rules"
    raw_log: list = []
    if mode == "llm":
        batch = int(os.getenv("EPISODES_PER_CALL", "3"))
        raw_cands = llm_candidates(eps, llm, show, batch, progress, raw_log)
    else:
        raw_cands = rule_candidates(eps, sig)

    progress("validate", 0.0, f"Validating {len(raw_cands)} candidates against the script")
    promos, rejected = [], []
    for c in raw_cands:
        p, why = build_promo(c, by_ep, sig)
        if p is None:
            rejected.append({"title": c.get("title"), "segments": c.get("segments"), "reason": why})
            continue
        p["score"] = score(p, len(eps))
        promos.append(p)
    ranked, dropped = dedupe_and_rank(promos)

    plan = {}
    if mode == "llm" and ranked:
        progress("rank", 0.5, "Editor pass: deciding posting order")
        try:
            plan = editor_plan(ranked, llm, show)
        except LLMError as e:
            plan = {"error": str(e)}
        order = {o.get("id"): i + 1 for i, o in enumerate(plan.get("post_order", []) or [])}
        for p in ranked:
            p["post_position"] = order.get(p["id"])
            p["post_reason"] = next((o.get("reason") for o in plan.get("post_order", []) if o.get("id") == p["id"]), None)

    q = quality(eps, raw_cands, ranked, rejected, dropped)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{mode}"
    meta = {"run_id": run_id, "show": show, "mode": mode, "data_dir": str(data_dir),
            "model": llm.model if mode == "llm" else None,
            "provider": llm.describe()["provider"] if mode == "llm" else None, "llm_usage": llm.usage,
            "elapsed_sec": round(time.time() - t0, 1), "created": datetime.now().isoformat(timespec="seconds")}
    out = Path(out_root) / run_id
    progress("write", 0.9, "Writing outputs")
    write_outputs(out, meta, ranked, q, plan, raw_log, raw_cands, dropped)
    progress("done", 1.0, f"Done: {len(ranked)} promos")
    return out
