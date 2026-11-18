"""Keystroke planning for the auto-typer (pure Python: no Windows, no Tk).

plan_typing() turns the source text into a list of Steps. A Step is one
keystroke unit -- a whole grapheme cluster such as "é", "e" + U+0301 or a
multi-part emoji, or a single Backspace -- plus the wait before it. Waits are
stored as multiples of the slider's base delay, so moving the slider mid-run
takes effect on the next keystroke.

The rhythm imitates a person copy-typing: a slower warm-up, common letter pairs
and double letters rolling off quickly, same-finger pairs, rare letters, digits
and Shift-capitals slower, frequent short words in fast bursts, a glance back at
the source every few words, momentary stalls, now and then a longer pause,
pauses after commas, sentences and paragraphs, and a gently drifting tempo.
Symbols and non-ASCII characters always take 1.5-2.5x the base delay.
Pace.base_ms() picks the base delay so the whole run averages the chosen WPM.

simulate() replays a plan on a plain buffer; the unit tests use it to prove that
every plan ends with exactly the source text, typos and all.
"""

from __future__ import annotations

import random
import unicodedata
from dataclasses import dataclass

TYPE = "type"
BACKSPACE = "backspace"

# Keystroke delays, as multiples of the base (ordinary-letter) delay.
LETTER_SPREAD = 0.25          # sigma of the log-normal jitter on ordinary keys
LETTER_RANGE = (0.5, 1.45)    # ordinary keys always stay below the symbol range
BURST_RANGE = (0.35, 0.6)
SLOW_RANGE = (1.5, 2.5)       # symbols and every non-ASCII grapheme
BACKSPACE_RANGE = (0.45, 0.8)
BURST_CHANCE = 0.06           # chance that any other word starts a fast burst ...
BURST_KEYS = (3, 7)           # ... lasting this many keystrokes
COMMON_WORD_BURST = 0.5       # chance a very common word is typed as one fast burst
HESITATE_CHANCE = 0.2         # chance of a short hesitation before a long word ...
HESITATE_MIN_LETTERS = 8      # ... of at least this many letters
GLANCE_WORDS = (3, 7)         # words typed between glances back at the source
THINK_CHANCE = 0.006          # chance per word of a longer "lost my place" pause
STALL_CHANCE = 0.03           # chance per mid-word letter of a momentary stall
WARMUP_KEYS = 20              # the first keystrokes are up to 30% slower
TEMPO_RANGE = (0.85, 1.15)    # slow drift of the typist's overall pace

# Extra pauses added on top of the key delay:
# (min factor, max factor, min extra ms, max extra ms).
PAUSES = {
    "sentence": (2.5, 4.5, 80, 250),     # whitespace after . ! ? …
    "paragraph": (5.0, 8.0, 250, 600),   # first key after a blank line
    "line": (1.5, 3.0, 0, 0),            # first key after a single line break
    "comma": (0.5, 1.4, 0, 100),         # whitespace after , ; :
    "word": (0.8, 2.0, 0, 120),          # hesitation before a long word
    "glance": (0.8, 2.0, 150, 450),      # look back at the source every few words
    "think": (3.0, 6.0, 600, 1500),      # rare longer pause
    "stall": (0.3, 1.0, 0, 60),          # momentary hesitation on a key
    "notice": (2.0, 3.5, 120, 300),      # typo stays on screen, then Backspace
    "resume": (0.5, 1.2, 0, 0),          # after erasing, before retyping
}

TYPO_RATE = 0.03              # chance per eligible letter (lowercase, mid-word)
TYPO_GAP = (10, 25)           # keystroke units between two typos
TYPO_KINDS = ("neighbor", "swap", "skip", "double", "shift")
TYPO_WEIGHTS = {"neighbor": 40, "swap": 25, "skip": 15, "double": 12, "shift": 8}

# The only symbols typed at ordinary speed: frequent, unshifted prose punctuation.
# Every other printable ASCII symbol -- including the shifted : ! ? " and all
# brackets -- is a slow symbol, like every non-ASCII character.
PROSE_PUNCTUATION = frozenset(".,;'-")
SENTENCE_ENDS = frozenset(".!?…")
CLAUSE_ENDS = frozenset(",;:")
CLOSERS = frozenset("\"')]}”’»")

# Letter pairs fluent typists produce fastest (the most frequent English bigrams).
FAST_BIGRAMS = frozenset(
    "th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se ha as ou "
    "io le ve co me de hi ri ro ic ne ea ra ce li ch ll be ma si om ur".split()
)
COMMON_WORDS = frozenset(
    "the of and to a in is it you that he was for on are as with his they at be this "
    "have from or one had by but not what all were we when your can said there use an "
    "each which she do how their if will up about out many then them so some her would "
    "make like him into has more go see no way could my than been who its now".split()
)

RARE_LETTERS = frozenset("qzxjkv")

ZWJ = "‍"
_KEY_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
# Touch-typing finger for each letter (0-3 left hand pinky..index, 4-7 right hand index..pinky).
_FINGERS = {ch: f for f, keys in enumerate(
    ("qaz", "wsx", "edc", "rfvtgb", "yhnujm", "ik", "ol", "p")) for ch in keys}


@dataclass(frozen=True)
class Step:
    """One keystroke unit and the wait before it."""

    action: str                # TYPE or BACKSPACE
    text: str = ""             # grapheme cluster to type ("" for BACKSPACE)
    key_factor: float = 1.0    # keystroke delay, in multiples of the base delay
    timing: str = "letter"     # letter | burst | slow | backspace
    pause: str = ""            # "" or a key of PAUSES
    pause_factor: float = 0.0  # extra pause, in multiples of the base delay
    pause_ms: float = 0.0      # extra fixed pause, in milliseconds
    typo: bool = False         # typed now, erased by a later Backspace

    def delay_ms(self, base_ms: float) -> float:
        return (self.key_factor + self.pause_factor) * base_ms + self.pause_ms


@dataclass(frozen=True)
class Pace:
    """Totals of a plan, used to turn an overall WPM into the base delay."""

    chars: int        # source characters (grapheme clusters) the plan types
    factors: float    # sum of key and pause factors over all steps
    fixed_ms: float   # sum of fixed pause milliseconds

    @classmethod
    def of(cls, steps: list[Step]) -> Pace:
        return cls(
            chars=sum(1 for s in steps if s.action == TYPE and not s.typo),
            factors=sum(s.key_factor + s.pause_factor for s in steps),
            fixed_ms=sum(s.pause_ms for s in steps),
        )

    def base_ms(self, wpm: float) -> float:
        """Base delay that makes the whole plan -- pauses and corrections included --
        take as long as typing it at `wpm` (standard 5 characters per word)."""
        per_char = 12_000 / wpm
        if not self.chars or not self.factors:
            return per_char
        base = (self.chars * per_char - self.fixed_ms) / self.factors
        return max(base, 0.25 * per_char)  # very high WPM with many pauses: best effort


# --- grapheme clusters --------------------------------------------------------

def _is_extend(ch: str) -> bool:
    """Code points that attach to the character before them."""
    cp = ord(ch)
    return (
        unicodedata.category(ch) in ("Mn", "Me", "Mc")  # combining marks, keycap
        or 0xFE00 <= cp <= 0xFE0F      # variation selectors (text/emoji style)
        or 0x1F3FB <= cp <= 0x1F3FF    # emoji skin-tone modifiers
        or 0xE0020 <= cp <= 0xE007F    # tag characters (subdivision flags)
        or 0xE0100 <= cp <= 0xE01EF    # variation selectors supplement
        or cp == 0x200C                # zero-width non-joiner
    )


def _is_pictographic(ch: str) -> bool:
    return 0x1F000 <= ord(ch) <= 0x1FAFF or unicodedata.category(ch) == "So"


def _is_regional_indicator(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def split_graphemes(text: str) -> list[str]:
    """Split text into user-perceived characters (grapheme clusters).

    A pragmatic subset of Unicode UAX #29 covering what matters for timing:
    letters with combining accents, emoji with skin tones, variation selectors
    or keycaps, ZWJ emoji sequences, flag pairs, tag-sequence flags and CR LF.
    """
    clusters = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        j = i + 1
        if ch == "\r" and j < n and text[j] == "\n":
            j += 1
        elif unicodedata.category(ch) != "Cc":  # control characters stand alone
            if _is_regional_indicator(ch) and j < n and _is_regional_indicator(text[j]):
                j += 1
            while j < n:
                c = text[j]
                if c == ZWJ:
                    j += 1
                    if j < n and _is_pictographic(ch) and _is_pictographic(text[j]):
                        j += 1
                elif _is_extend(c):
                    j += 1
                else:
                    break
        clusters.append(text[i:j])
        i = j
    return clusters


def is_slow_unit(unit: str) -> bool:
    """True for symbols such as @ # % + and for any non-ASCII grapheme."""
    if any(ord(c) > 0x7F for c in unit):
        return True
    return (
        len(unit) == 1
        and unit.isprintable()
        and not unit.isalnum()
        and not unit.isspace()
        and unit not in PROSE_PUNCTUATION
    )


def _is_letter(unit: str) -> bool:
    return len(unit) == 1 and unit.isascii() and unit.isalpha()


def _is_lower(unit: str) -> bool:
    return len(unit) == 1 and "a" <= unit <= "z"


def _is_newline(unit: str) -> bool:
    return unit in ("\n", "\r\n", "\r")


def _ends_sentence(units: list[str], j: int) -> bool:
    unit = units[j]
    if unit in CLOSERS and j > 0:  # e.g. the quote in: He said "hi."
        unit = units[j - 1]
    return unit in SENTENCE_ENDS


def _word_at(units: list[str], i: int) -> str:
    j = i
    while j < len(units) and _is_letter(units[j]):
        j += 1
    return "".join(units[i:j])


def _keyboard_neighbors() -> dict[str, str]:
    """Adjacent keys on a QWERTY layout, used for substitution typos."""
    neighbors = {}
    for r, row in enumerate(_KEY_ROWS):
        for c, ch in enumerate(row):
            around = [(r, c - 1), (r, c + 1), (r - 1, c), (r - 1, c + 1), (r + 1, c - 1), (r + 1, c)]
            neighbors[ch] = "".join(
                _KEY_ROWS[rr][cc] for rr, cc in around
                if 0 <= rr < len(_KEY_ROWS) and 0 <= cc < len(_KEY_ROWS[rr])
            )
    return neighbors


NEIGHBORS = _keyboard_neighbors()


# --- planning -----------------------------------------------------------------

def plan_typing(
    text: str,
    corrections: bool = True,
    rng: random.Random | None = None,
    typo_rate: float = TYPO_RATE,
    typo_kinds: tuple[str, ...] = TYPO_KINDS,
) -> list[Step]:
    """Plan every keystroke needed to type `text`, with optional typo fixes."""
    planner = _Planner(split_graphemes(text), rng or random.Random(), corrections, typo_rate, typo_kinds)
    return planner.plan()


class _Planner:
    def __init__(self, units, rng, corrections, typo_rate, typo_kinds):
        self.units = units
        self.rng = rng
        self.corrections = corrections
        self.typo_rate = typo_rate
        self.typo_kinds = typo_kinds
        self.steps: list[Step] = []
        self.burst_left = 0
        self.typo_gap = 0
        self.tempo = 1.0
        self.prev_key = ""  # previous keystroke, for finger/bigram timing
        self.keys_typed = 0
        self.words_until_glance = rng.randint(*GLANCE_WORDS)

    def plan(self) -> list[Step]:
        i = 0
        while i < len(self.units):
            self._drift_tempo()
            pause = self._pause_before(i)
            if pause:
                self.burst_left = 0
                if pause[0] in ("sentence", "line", "paragraph"):  # these double as a glance
                    self.words_until_glance = self.rng.randint(*GLANCE_WORDS)
            if pause is None or pause[0] in ("line", "paragraph"):
                word_pause = self._word_start(i)  # may also start a burst
                pause = pause or word_pause
            if pause is None and self._stalls(i):
                pause = self._pause("stall")
            consumed = 0 if pause else self._maybe_typo(i)
            if consumed:
                i += consumed
                continue
            self._type(self.units[i], pause)
            self.typo_gap -= 1
            i += 1
        return self.steps

    # Timing ------------------------------------------------------------------

    def _pause(self, name: str) -> tuple[str, float, float]:
        fmin, fmax, ms_min, ms_max = PAUSES[name]
        return name, self.rng.uniform(fmin, fmax), self.rng.uniform(ms_min, ms_max)

    def _drift_tempo(self) -> None:
        """Random walk with a pull back to 1.0: the typist speeds up and slows down."""
        self.tempo += self.rng.gauss(0.0, 0.015) + 0.03 * (1.0 - self.tempo)
        self.tempo = min(max(self.tempo, TEMPO_RANGE[0]), TEMPO_RANGE[1])

    def _pause_before(self, i: int):
        """Think-pause before unit i: after sentence/clause ends and line breaks."""
        if i == 0:
            return None
        units, cur = self.units, self.units[i]
        if _is_newline(units[i - 1]):
            if _is_newline(cur):
                return None  # still inside a run of line breaks
            run, j = 0, i - 1
            while j >= 0 and _is_newline(units[j]):
                run, j = run + 1, j - 1
            return self._pause("paragraph" if run >= 2 else "line")
        if cur.isspace():
            if _ends_sentence(units, i - 1):
                return self._pause("sentence")
            if units[i - 1] in CLAUSE_ENDS:
                return self._pause("comma")
        return None

    def _word_start(self, i: int):
        """At the first letter of a word: maybe burst through it, maybe pause first.

        Copy-typists glance back at the source every few words, now and then lose
        their place for a moment, and slow down before long words.
        """
        units = self.units
        if not units[i].isalnum() or (i > 0 and not units[i - 1].isspace()):
            return None
        word = _word_at(units, i)
        if self.burst_left == 0 and word:
            if word.lower() in COMMON_WORDS and self.rng.random() < COMMON_WORD_BURST:
                self.burst_left = len(word) + 1  # the word and the space after it
            elif self.rng.random() < BURST_CHANCE:
                self.burst_left = self.rng.randint(*BURST_KEYS)
        self.words_until_glance -= 1
        if self.rng.random() < THINK_CHANCE:
            pause = self._pause("think")
        elif self.words_until_glance <= 0:
            pause = self._pause("glance")  # a burst may follow: the chunk just read
        elif len(word) >= HESITATE_MIN_LETTERS and self.rng.random() < HESITATE_CHANCE:
            self.burst_left = 0
            return self._pause("word")
        else:
            return None
        self.words_until_glance = self.rng.randint(*GLANCE_WORDS)
        return pause

    def _stalls(self, i: int) -> bool:
        units = self.units
        return (i > 0 and _is_letter(units[i]) and _is_letter(units[i - 1])
                and self.rng.random() < STALL_CHANCE)

    def _key_timing(self, unit: str) -> tuple[float, str]:
        if is_slow_unit(unit):
            self.burst_left = 0
            return self.rng.uniform(*SLOW_RANGE), "slow"
        if self.burst_left > 0:
            self.burst_left -= 1
            return self.rng.uniform(*BURST_RANGE), "burst"
        factor = self.rng.lognormvariate(0.0, LETTER_SPREAD) * self.tempo
        prev, cur = self.prev_key.lower(), unit.lower()
        if _is_letter(prev) and _is_letter(cur):
            if prev == cur:
                factor *= self.rng.uniform(0.65, 0.85)   # double letter: same key twice
            elif prev + cur in FAST_BIGRAMS:
                factor *= self.rng.uniform(0.72, 0.88)   # well-practised letter pair
            elif _FINGERS[prev] == _FINGERS[cur]:
                factor *= self.rng.uniform(1.1, 1.25)    # same finger has to move
            elif (_FINGERS[prev] < 4) != (_FINGERS[cur] < 4):
                factor *= 0.92                           # hands alternate
        elif prev.isspace() and _is_letter(cur):
            factor *= self.rng.uniform(1.05, 1.2)        # first letter of a word
        if unit == " " and _is_letter(prev):
            factor *= self.rng.uniform(0.8, 1.0)         # quick thumb on the space bar
        if cur in RARE_LETTERS or unit.isdigit():
            factor *= self.rng.uniform(1.05, 1.25)       # unfamiliar reach
        if unit.isupper():
            factor *= self.rng.uniform(1.15, 1.35)       # reach for Shift
        if self.keys_typed < WARMUP_KEYS:
            factor *= 1 + 0.3 * (1 - self.keys_typed / WARMUP_KEYS)  # warming up
        return min(max(factor, LETTER_RANGE[0]), LETTER_RANGE[1]), "letter"

    # Steps -------------------------------------------------------------------

    def _type(self, unit: str, pause=None, typo: bool = False) -> None:
        factor, timing = self._key_timing(unit)
        name, pause_factor, pause_ms = pause or ("", 0.0, 0.0)
        self.steps.append(Step(TYPE, unit, factor, timing, name, pause_factor, pause_ms, typo))
        self.prev_key = unit
        self.keys_typed += 1

    def _backspace(self, pause=None) -> None:
        name, pause_factor, pause_ms = pause or ("", 0.0, 0.0)
        factor = self.rng.uniform(*BACKSPACE_RANGE)
        self.steps.append(Step(BACKSPACE, "", factor, "backspace", name, pause_factor, pause_ms))
        self.prev_key = ""

    def _maybe_typo(self, i: int) -> int:
        """Maybe plan a typo at unit i and its fix; return how many units it covers.

        Typos only hit lowercase ASCII letters in the middle of a word, so every
        Backspace erases exactly one plain letter that the typo itself typed.
        """
        units = self.units
        if (
            not self.corrections
            or self.typo_gap > 0
            or i == 0
            or not _is_lower(units[i])
            or not _is_letter(units[i - 1])
            or self.rng.random() >= self.typo_rate
        ):
            return 0
        cur, prev = units[i], units[i - 1]
        nxt = units[i + 1] if i + 1 < len(units) else ""
        word_start = i == 1 or not _is_letter(units[i - 2])
        allowed = {
            "neighbor": True,
            "swap": _is_lower(nxt) and nxt != cur,
            "skip": _is_lower(nxt) and nxt != cur,
            "double": nxt != cur,
            "shift": prev.isupper() and word_start,  # Shift held too long: "THe"
        }
        kinds = [k for k in self.typo_kinds if allowed.get(k)]
        if not kinds:
            return 0
        kind = self.rng.choices(kinds, [TYPO_WEIGHTS[k] for k in kinds])[0]
        if kind == "swap":        # "the" -> "teh", erase "eh", type "he"
            wrong, right = [nxt, cur], [cur, nxt]
        elif kind == "skip":      # "the" -> "te", erase "e", type "he"
            wrong, right = [nxt], [cur, nxt]
        elif kind == "double":    # "the" -> "thh", erase one "h"
            wrong, right = [cur, cur], [cur]
        elif kind == "shift":     # "The" -> "THe", erase "H", type "h"
            wrong, right = [cur.upper()], [cur]
        else:                     # neighbouring key, sometimes noticed a letter or two late
            late = 0
            while late < 2 and i + 1 + late < len(units) and _is_lower(units[i + 1 + late]):
                late += 1
            late = min(late, self.rng.choice((0, 0, 0, 1, 1, 2)))
            wrong = [self.rng.choice(NEIGHBORS[cur])] + units[i + 1:i + 1 + late]
            right = units[i:i + 1 + late]
        keep = 0  # leading keystrokes that were right after all
        while keep < min(len(wrong), len(right)) and wrong[keep] == right[keep]:
            keep += 1
        for n, unit in enumerate(wrong):
            self._type(unit, typo=n >= keep)
        self.burst_left = 0
        for n in range(len(wrong) - keep):
            self._backspace(self._pause("notice") if n == 0 else None)
        for n, unit in enumerate(right[keep:]):
            self._type(unit, self._pause("resume") if n == 0 else None)
        self.typo_gap = self.rng.randint(*TYPO_GAP)
        return len(right)


def simulate(steps: list[Step]) -> str:
    """Replay a plan the way the editor applies it (Backspace removes one code point)."""
    buf: list[str] = []
    for step in steps:
        if step.action == BACKSPACE:
            buf.pop()
        else:
            buf.extend(step.text)
    return "".join(buf)
