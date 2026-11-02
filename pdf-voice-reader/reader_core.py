"""PDF text extraction, sentence chunking and word/audio alignment.

Nothing in this module touches the GUI or audio, so it can be tested on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

# Words that end with "." but normally do not end a sentence.
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "no", "nos", "vs", "etc",
    "e.g", "i.e", "fig", "figs", "eq", "ref", "vol", "pp", "p", "approx", "dept",
    "govt", "ltd", "pvt", "co", "inc", "corp", "rs", "sec", "ch", "art", "cl",
    "viz", "cf", "al", "u.s", "a.m", "p.m", "jan", "feb", "mar", "apr", "jun",
    "jul", "aug", "sep", "sept", "oct", "nov", "dec", "smt", "shri", "hon",
}

ABBREV_NORM = {re.sub(r"[^0-9a-z]+", "", a) for a in ABBREVIATIONS}

SENTENCE_END = re.compile(r"[.!?।]['\")\]’”]*$")  # । = Devanagari danda
SOFT_BREAK = re.compile(r"[,;:–—]['\")\]’”]*$")
_NORM = re.compile(r"[^0-9a-z]+")

SOFT_LIMIT = 260   # characters: try to break at a comma after this
HARD_LIMIT = 420   # characters: always break after this


def norm(s: str) -> str:
    return _NORM.sub("", s.lower())


@dataclass
class Word:
    page: int
    rect: tuple  # (x0, y0, x1, y1) in PDF points
    text: str
    block: int
    line: int


@dataclass
class Chunk:
    """A sentence-sized piece of text that is synthesised as one audio clip."""
    index: int
    page: int
    word_ids: list = field(default_factory=list)  # indexes into Document.words
    text: str = ""


class Document:
    def __init__(self, path: str):
        self.path = path
        self.pdf = pymupdf.open(path)
        if self.pdf.needs_pass:
            raise ValueError("This PDF is password protected.")
        self.page_count = self.pdf.page_count
        self.words: list[Word] = []
        self.chunks: list[Chunk] = []
        self.page_first_chunk: dict[int, int] = {}
        self._extract()
        self._build_chunks()

    # ------------------------------------------------------------------ text
    def _extract(self):
        for pno in range(self.page_count):
            page = self.pdf[pno]
            for x0, y0, x1, y1, text, block, line, _wno in page.get_text("words"):
                text = text.strip()
                if text:
                    self.words.append(Word(pno, (x0, y0, x1, y1), text, block, line))

    def _build_chunks(self):
        words = self.words
        cur: Chunk | None = None
        pieces: list[str] = []
        glue_next = False

        def close():
            nonlocal cur, pieces
            if cur is not None and cur.word_ids:
                cur.text = "".join(pieces).strip()
                self.chunks.append(cur)
            cur, pieces = None, []

        for i, w in enumerate(words):
            if cur is None:
                cur = Chunk(index=len(self.chunks), page=w.page)
                glue_next = False
            cur.word_ids.append(i)

            nxt = words[i + 1] if i + 1 < len(words) else None
            text = w.text
            # Re-join words hyphenated across a line break: "inter-" + "national".
            hyphen_join = (
                nxt is not None and text.endswith("-") and len(text) > 2
                and nxt.page == w.page and nxt.block == w.block and nxt.line != w.line
                and nxt.text[:1].islower()
            )
            if hyphen_join:
                text = text[:-1]
            pieces.append(("" if glue_next or not pieces else " ") + text)
            glue_next = hyphen_join

            if nxt is None:
                break
            length = sum(len(p) for p in pieces)
            stripped = w.text.rstrip("'\")]’”")
            is_abbrev = stripped.endswith(".") and norm(stripped[:-1]) in ABBREV_NORM
            single_initial = len(stripped) == 2 and stripped[0].isupper() and stripped[1] == "."
            ends_sentence = (
                SENTENCE_END.search(w.text) is not None and not is_abbrev and not single_initial
            )
            if (
                nxt.page != w.page
                or nxt.block != w.block          # new paragraph / heading
                or ends_sentence
                or (length > SOFT_LIMIT and SOFT_BREAK.search(w.text))
                or length > HARD_LIMIT
            ):
                if not hyphen_join:
                    close()
        close()

        for c in self.chunks:
            self.page_first_chunk.setdefault(c.page, c.index)

    # ------------------------------------------------------------- lookups
    def first_chunk_at_or_after_page(self, page: int) -> int | None:
        for p in range(page, self.page_count):
            if p in self.page_first_chunk:
                return self.page_first_chunk[p]
        return None

    def word_at(self, page: int, x: float, y: float) -> int | None:
        """Index of the word under (x, y) in PDF points, or the nearest one."""
        best, best_d = None, 1e18
        for i, w in enumerate(self.words):
            if w.page != page:
                continue
            x0, y0, x1, y1 = w.rect
            if x0 <= x <= x1 and y0 <= y <= y1:
                return i
            dx = max(x0 - x, 0, x - x1)
            dy = max(y0 - y, 0, y - y1)
            d = dx * dx + dy * dy * 4
            if d < best_d:
                best, best_d = i, d
        return best if best_d < 40 * 40 else None

    def chunk_of_word(self, word_id: int) -> int | None:
        lo, hi = 0, len(self.chunks) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            c = self.chunks[mid]
            if word_id < c.word_ids[0]:
                hi = mid - 1
            elif word_id > c.word_ids[-1]:
                lo = mid + 1
            else:
                return mid
        return None

    def close(self):
        try:
            self.pdf.close()
        except Exception:
            pass


# ---------------------------------------------------------------- alignment
def align_word_times(chunk_words: list[str], boundaries: list[tuple[float, str]],
                     duration_ms: float) -> list[float]:
    """Return a start time (ms) for every PDF word in a chunk.

    `boundaries` are (offset_ms, spoken_word) pairs reported by the TTS engine.
    Spoken words are matched greedily to PDF words; words that could not be
    matched get interpolated times so the highlighter still moves smoothly.
    """
    n = len(chunk_words)
    times: list[float | None] = [None] * n
    normed = [norm(w) for w in chunk_words]
    j = 0
    for offset, spoken in boundaries:
        s = norm(spoken)
        if not s:
            continue
        for k in range(j, min(j + 6, n)):
            w = normed[k]
            if not w:
                continue
            if w == s or w.startswith(s) or s.startswith(w) or (len(s) > 2 and s in w):
                if times[k] is None:
                    times[k] = offset
                j = k + 1 if (w == s or s.startswith(w)) else k
                break
    return _fill(times, duration_ms, chunk_words)


def estimate_word_times(chunk_words: list[str], duration_ms: float) -> list[float]:
    """Spread words over the clip proportionally to their length."""
    return _fill([None] * len(chunk_words), duration_ms, chunk_words)


def _fill(times, duration_ms, chunk_words):
    n = len(times)
    if n == 0:
        return []
    weights = [len(w) + 2 for w in chunk_words]
    total = float(sum(weights)) or 1.0
    lead = min(150.0, duration_ms * 0.05)
    span = max(duration_ms - lead * 2, 1.0)
    est, acc = [], 0.0
    for wgt in weights:
        est.append(lead + span * acc / total)
        acc += wgt
    known = [i for i, t in enumerate(times) if t is not None]
    if not known:
        return est
    out = list(times)
    # Before first known / after last known: use estimates, clamped for monotonicity.
    for i in range(known[0]):
        out[i] = min(est[i], out[known[0]])
    for a, b in zip(known, known[1:]):
        for i in range(a + 1, b):
            frac = (i - a) / (b - a)
            out[i] = out[a] + (out[b] - out[a]) * frac
    last = known[-1]
    for i in range(last + 1, n):
        out[i] = max(est[i], out[last] + 1)
    # enforce monotonic order
    for i in range(1, n):
        if out[i] < out[i - 1]:
            out[i] = out[i - 1]
    return out
