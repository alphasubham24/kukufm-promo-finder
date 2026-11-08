"""Rule-based hook / cliffhanger signals (no LLM).

Cheap, explainable, and independent of the model, so they act as a second
opinion: where rules and the LLM disagree strongly, a human should look.
Scores are 0..1 per block.
"""
from __future__ import annotations

import re

from .parser import Block, Episode

HOOK_WORDS = {
    # physical / visceral
    "slap": 1.0, "slaps": 1.0, "slapped": 1.0, "punch": 0.8, "strike": 0.6, "strikes": 0.6,
    "blood": 0.7, "bloody": 0.7, "knife": 0.8, "dagger": 0.8, "sword": 0.6, "gun": 0.8, "pistol": 0.8,
    "grabs": 0.5, "shoves": 0.6, "throws": 0.5, "collapses": 0.7, "kiss": 0.7, "kisses": 0.7,
    "tears": 0.4, "screams": 0.8, "scream": 0.8, "gasps": 0.6, "shatters": 0.7, "slams": 0.6,
    "kneel": 0.6, "kneels": 0.6, "chokes": 0.8, "poison": 0.9, "fire": 0.5, "faints": 0.7,
    # dramatic stakes
    "betray": 0.8, "betrayed": 0.8, "divorce": 0.8, "pregnant": 0.9, "dead": 0.7, "die": 0.6,
    "kill": 0.9, "murder": 0.9, "secret": 0.6, "lie": 0.5, "liar": 0.7, "disown": 0.9,
    "disowned": 0.9, "marry": 0.6, "married": 0.6, "wife": 0.4, "husband": 0.4, "duke": 0.6,
    "humiliate": 0.7, "humiliation": 0.7, "how dare": 1.0, "get out": 0.8, "never": 0.3,
    "mistress": 0.8, "illegitimate": 0.8, "beggar": 0.6, "cripple": 0.8, "scar": 0.5,
}
INTENSE_EMOTIONS = {
    "furious": 1.0, "enraged": 1.0, "screaming": 1.0, "shocked": 0.8, "horrified": 0.9,
    "sobbing": 0.8, "menacing": 0.8, "venomous": 0.9, "seething": 0.9, "cruel": 0.7,
    "gloating": 0.7, "mocking": 0.7, "smug": 0.6, "cold": 0.5, "defiant": 0.6, "desperate": 0.6,
    "threatening": 0.9, "outraged": 0.9, "panicked": 0.8, "trembling": 0.5, "icy": 0.6,
    "contemptuous": 0.7, "dangerous": 0.8, "livid": 1.0, "hysterical": 0.9,
}
SFX_HOOK = ["sting", "slam", "crash", "thud", "gasp", "smack", "crack", "shatter", "boom", "impact", "slap"]
CLIFF_WORDS = ["cliffhanger", "ominous", "cut to black", "freeze", "swell", "drone", "suspense",
               "tension", "silence falls", "hangs in the air", "unanswered"]
CLIFF_LINE = re.compile(r"(\?|\.\.\.|—|–|-)\s*$")


def _kw(text: str, table: dict) -> float:
    t = text.lower()
    best = 0.0
    for k, w in table.items():
        if re.search(rf"\b{re.escape(k)}\b", t):
            best = max(best, w)
    return best


def hook_score(b: Block) -> float:
    if b.kind == "CAMERA":
        return 0.0
    s = 0.6 * _kw(b.text, HOOK_WORDS)
    if b.kind == "DIALOGUE":
        s += 0.4 * _kw(b.emotion, INTENSE_EMOTIONS)
        if "!" in b.text:
            s += 0.15
        if len(b.text) < 60:  # short punchy lines read faster in the first 3s
            s += 0.05
    elif b.kind == "SOUND FX":
        s += 0.4 * (1.0 if any(w in b.text.lower() for w in SFX_HOOK) else 0.0)
    elif b.kind == "ACTION":
        s += 0.1
    return min(1.0, s)


def cliff_score(b: Block, ep: Episode) -> float:
    if b.kind == "CAMERA" and "cut to black" not in b.text.lower():
        return 0.0
    t = b.text.lower()
    s = 0.0
    if any(w in t for w in CLIFF_WORDS):
        s += 0.6
    if b.kind == "DIALOGUE" and CLIFF_LINE.search(b.text.strip('"')):
        s += 0.3
    if b.kind == "DIALOGUE":
        s += 0.3 * _kw(b.emotion, INTENSE_EMOTIONS)
    s += 0.25 * _kw(b.text, HOOK_WORDS)
    # episode endings are written as cliffhangers in this format
    if ep.duration and ep.duration - b.end <= 8:
        s += 0.35
    return min(1.0, s)


def annotate(episodes: list[Episode]) -> dict[str, dict]:
    """block_id -> {hook, cliff}"""
    out = {}
    for ep in episodes:
        for b in ep.blocks:
            out[b.id] = {"hook": round(hook_score(b), 3), "cliff": round(cliff_score(b, ep), 3)}
    return out
