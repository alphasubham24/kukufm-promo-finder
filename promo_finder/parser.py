"""Deterministic screenplay parser.

Turns each episode .txt into a list of timestamped blocks. No LLM involved:
every timestamp the tool outputs is traced back to a block parsed here, so the
model can never invent a timestamp.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

TS_RE = re.compile(r"Start:\s*\((\d+):(\d+(?:\.\d+)?)\)\s*\|\s*End:\s*\((\d+):(\d+(?:\.\d+)?)\)")
HEADER_RE = re.compile(r"^\*\*(.+?):\*\*\s*$")
SPEAKER_RE = re.compile(r"^(.+?)\s*\((.*)\)\s*$")
SCENE_RE = re.compile(r"^SCENE\s+(\d+)")
EP_NUM_RE = re.compile(r"(\d+)")

NON_DIALOGUE = {"CAMERA", "ACTION", "SOUND FX", "SFX", "VO", "TEXT", "SUPER"}


def ts_to_sec(m: str, s: str) -> float:
    return int(m) * 60 + float(s)


def sec_to_ts(x: float) -> str:
    m, s = divmod(max(0.0, x), 60)
    return f"{int(m):02d}:{s:05.2f}"


@dataclass
class Block:
    id: str            # e.g. "E03-B045" — stable handle the LLM refers to
    ep: int
    idx: int
    scene: int
    scene_heading: str
    kind: str          # CAMERA | ACTION | SOUND FX | DIALOGUE
    speaker: str       # for dialogue
    emotion: str       # parenthetical tag on dialogue, e.g. "Furious"
    start: float
    end: float
    text: str

    def compact(self) -> str:
        """One-line form sent to the LLM (CAMERA blocks are usually dropped)."""
        t = f"[{self.id} {sec_to_ts(self.start)}-{sec_to_ts(self.end)}]"
        if self.kind == "DIALOGUE":
            emo = f" ({self.emotion})" if self.emotion else ""
            return f"{t} {self.speaker}{emo}: {self.text}"
        return f"{t} <{self.kind}> {self.text}"


@dataclass
class Episode:
    ep: int
    title: str
    logline: str
    continuity: str
    duration: float
    source: str
    blocks: list[Block] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def parse_episode(path: Path, fallback_num: int) -> Episode:
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()

    title = logline = continuity = ""
    for ln in lines[:15]:
        if ln.startswith("EPISODE NUMBER:"):
            title = ln.split(":", 1)[1].strip()
        elif ln.startswith("EPISODE LOGLINE:"):
            logline = ln.split(":", 1)[1].strip()
        elif ln.startswith("CONTINUITY NOTES:"):
            continuity = ln.split(":", 1)[1].strip()

    m = EP_NUM_RE.search(title) or EP_NUM_RE.search(path.stem)
    ep_num = int(m.group(1)) if m else fallback_num
    # file names look like "episode-001-Episode 1671703" -> prefer the first number
    m2 = re.match(r"episode-(\d+)", path.stem)
    if m2:
        ep_num = int(m2.group(1))

    blocks: list[Block] = []
    scene, heading = 0, ""
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        sm = SCENE_RE.match(ln)
        if sm:
            scene = int(sm.group(1))
        elif ln.startswith("Scene heading:"):
            heading = ln.split(":", 1)[1].strip()
        hm = HEADER_RE.match(ln)
        if hm and i + 1 < len(lines):
            tm = TS_RE.search(lines[i + 1])
            if tm:
                label = hm.group(1).strip()
                # body = following non-empty lines until blank line / next header
                body = []
                j = i + 2
                while j < len(lines) and lines[j].strip() and not HEADER_RE.match(lines[j].strip()):
                    body.append(lines[j].strip())
                    j += 1
                text = " ".join(body).strip()
                kind, speaker, emotion = label.upper(), "", ""
                if kind not in NON_DIALOGUE:
                    kind = "DIALOGUE"
                    spm = SPEAKER_RE.match(label)
                    speaker, emotion = (spm.group(1), spm.group(2)) if spm else (label, "")
                    text = text.strip('"').strip("“”")
                start = ts_to_sec(tm.group(1), tm.group(2))
                end = ts_to_sec(tm.group(3), tm.group(4))
                blocks.append(Block(
                    id=f"E{ep_num:02d}-B{len(blocks):03d}", ep=ep_num, idx=len(blocks),
                    scene=scene, scene_heading=heading, kind=kind, speaker=speaker.strip(),
                    emotion=emotion.strip(), start=start, end=max(end, start), text=text,
                ))
                i = j
                continue
        i += 1

    duration = max((b.end for b in blocks), default=0.0)
    for ln in lines:
        if ln.startswith("Scene end:"):
            mm = re.search(r"(\d+):(\d+(?:\.\d+)?)", ln)
            if mm:
                duration = max(duration, ts_to_sec(mm.group(1), mm.group(2)))
    return Episode(ep=ep_num, title=title or path.stem, logline=logline,
                   continuity=continuity, duration=duration, source=path.name, blocks=blocks)


def load_show(data_dir: str | Path) -> list[Episode]:
    d = Path(data_dir)
    files = sorted(p for p in d.rglob("*.txt") if not p.name.startswith("."))
    eps = [parse_episode(p, n + 1) for n, p in enumerate(files)]
    eps = [e for e in eps if e.blocks]
    eps.sort(key=lambda e: e.ep)
    return eps


def episode_text(ep: Episode, include_camera: bool = False) -> str:
    return "\n".join(b.compact() for b in ep.blocks if include_camera or b.kind != "CAMERA")
