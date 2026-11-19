"""Tiny retrieval layer over the equipment manuals.

Uses BM25 keyword ranking: no embedding API, no vector DB, instant start-up, and very good at
exact terms technicians say ("E4", "capacitor", "CX-300"). Swap search() for an embedding
search later if you want; the interface stays the same."""
import math
import re
from collections import Counter
from dataclasses import dataclass

from . import config

TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)?")
STOP = set("the a an and or of to in on for is are be with if it this that at by as from "
           "not no do does what how my i".split())


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOP]


@dataclass
class Chunk:
    source: str
    heading: str
    text: str
    tokens: list[str]


_chunks: list[Chunk] = []
_df: Counter = Counter()


def _load() -> None:
    _chunks.clear()
    _df.clear()
    for path in sorted(config.MANUALS_DIR.glob("*.md")):
        title, heading, buf = path.stem, "", []

        def flush():
            body = " ".join(buf).strip()
            if body:
                # Split long sections into ~2-3 sentence windows so answers stay short for voice.
                parts = re.split(r"(?<=[.])\s+(?=(?:Step \d|[-A-Z]))", body)
                window: list[str] = []
                for p in parts:
                    window.append(p)
                    if sum(len(w) for w in window) > 350:
                        _add(title, heading, " ".join(window))
                        window = []
                if window:
                    _add(title, heading, " ".join(window))

        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("## "):
                flush()
                heading, buf = line[3:].strip(), []
            elif line.strip():
                buf.append(line.strip())
        flush()
    for ch in _chunks:
        _df.update(set(ch.tokens))


def _add(source: str, heading: str, text: str) -> None:
    _chunks.append(Chunk(source, heading, text, tokenize(f"{source} {heading} {text}")))


def search(query: str, k: int = 3) -> list[dict]:
    if not _chunks:
        _load()
    q = tokenize(query)
    if not q:
        return []
    n = len(_chunks)
    avg = sum(len(c.tokens) for c in _chunks) / n
    scored = []
    for ch in _chunks:
        tf = Counter(ch.tokens)
        s = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - _df[term] + 0.5) / (_df[term] + 0.5))
            f = tf[term]
            s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(ch.tokens) / avg))
        if s > 0:
            scored.append((s, ch))
    scored.sort(key=lambda x: -x[0])
    return [{"source": c.source, "section": c.heading, "text": c.text, "score": round(s, 2)}
            for s, c in scored[:k]]
