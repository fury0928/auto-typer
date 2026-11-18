"""Unit tests for typing_plan (pure Python; runs on any OS).

    py -m unittest discover -s tests -v
"""

import os
import random
import statistics
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing_plan import (  # noqa: E402
    BACKSPACE, BURST_RANGE, LETTER_RANGE, SLOW_RANGE, TYPE,
    is_slow_unit, plan_typing, simulate, split_graphemes,
)

FAMILY = "👩‍👩‍👧"
ENGLAND = "🏴\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F"
SAMPLE = (
    "Hello world. Mail test@example.com #42 50% 3+4=7!\n\n"
    "Café, naïve, é and 😊 👍🏽 " + FAMILY + " 🇺🇸 1️⃣.\tTab.  Two spaces.\n"
    "Line two? Yes \"quoted.\" End.  \n"
)


def visible_states(steps):
    """Editor contents after each step."""
    states, buf = [], []
    for step in steps:
        if step.action == BACKSPACE:
            buf.pop()
        else:
            buf.extend(step.text)
        states.append("".join(buf))
    return states


class GraphemeTests(unittest.TestCase):
    def test_clusters(self):
        cases = {
            "abc": ["a", "b", "c"],
            "éx": ["é", "x"],
            "é": ["é"],
            "😊!": ["😊", "!"],
            "👍🏽": ["👍🏽"],
            FAMILY: [FAMILY],
            "🏳️‍🌈": ["🏳️‍🌈"],
            "🇺🇸🇫🇷": ["🇺🇸", "🇫🇷"],
            "1️⃣": ["1️⃣"],
            ENGLAND: [ENGLAND],
            "❤️": ["❤️"],
            "a\r\nb": ["a", "\r\n", "b"],
            "\ń": ["\n", "́"],
            "a‍b": ["a‍", "b"],
            "": [],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(split_graphemes(text), expected)

    def test_split_is_lossless(self):
        rng = random.Random(7)
        pool = "ab \n\t" + "́‍️⃣" + "😊👩🇺🇸🏽é中@"
        for _ in range(2000):
            text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 30)))
            self.assertEqual("".join(split_graphemes(text)), text)


class PlanTests(unittest.TestCase):
    def test_every_plan_reproduces_the_input_exactly(self):
        rng = random.Random(1)
        pool = (["e", "t", "a", "o", "n", "s", "h", "r"] * 6 + list("EQZ0123456789") + [" "] * 8
                + ["\n", "\n\n", "\t", ".", ",", "?", "@", "#", "%", "+", "é", "é", "😊", FAMILY,
                   "🇺🇸", "👍🏽", "中", " "])
        for trial in range(1500):
            text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 120)))
            for corrections in (True, False):
                for typo_rate in (0.03, 0.5, 1.0):
                    plan = plan_typing(text, corrections, random.Random(trial), typo_rate)
                    self.assertEqual(simulate(plan), text, (text, corrections, typo_rate))

    def test_backspace_only_erases_plain_letters_typed_by_a_typo(self):
        text = "the quick brown fox jumps over the lazy dog " * 30
        plan = plan_typing(text, True, random.Random(3), typo_rate=0.5)
        typed = []  # (char, was_typo) per code point in the editor
        for step in plan:
            if step.action == BACKSPACE:
                char, was_typo = typed.pop()
                self.assertTrue(was_typo and char.isascii() and char.isalpha())
            else:
                typed.extend((c, step.typo) for c in step.text)
        self.assertEqual("".join(c for c, _ in typed), text)
        self.assertFalse(any(was_typo for _, was_typo in typed))

    def test_teh_example(self):
        plan = plan_typing("the", True, random.Random(0), typo_rate=1.0, typo_kinds=("swap",))
        self.assertEqual(visible_states(plan), ["t", "te", "teh", "te", "t", "th", "the"])
        first_backspace = plan[3]
        self.assertEqual((first_backspace.action, first_backspace.pause), (BACKSPACE, "notice"))
        self.assertGreaterEqual(first_backspace.pause_factor, 2.0)

    def test_corrections_off_types_each_unit_once(self):
        plan = plan_typing(SAMPLE * 5, False, random.Random(2), typo_rate=1.0)
        self.assertTrue(all(s.action == TYPE and not s.typo for s in plan))
        self.assertEqual([s.text for s in plan], split_graphemes(SAMPLE * 5))

    def test_corrections_happen_occasionally(self):
        text = "the quick brown fox jumps over the lazy dog " * 40
        plan = plan_typing(text, True, random.Random(4))
        fixes = sum(1 for s in plan if s.pause == "notice")
        words = len(text.split())
        self.assertTrue(words / 40 <= fixes <= words / 4, fixes)

    def test_combined_graphemes_are_single_keystroke_units(self):
        plan = plan_typing(SAMPLE, False, random.Random(5))
        texts = [s.text for s in plan]
        for unit in ("é", "👍🏽", FAMILY, "🇺🇸", "1️⃣"):
            self.assertIn(unit, texts)

    def test_symbol_and_unicode_delays(self):
        for seed in range(20):
            for step in plan_typing(SAMPLE * 3, True, random.Random(seed), typo_rate=0.2):
                if step.action == BACKSPACE:
                    continue
                if is_slow_unit(step.text):
                    self.assertEqual(step.timing, "slow")
                    self.assertTrue(SLOW_RANGE[0] <= step.key_factor <= SLOW_RANGE[1])
                else:
                    self.assertIn(step.timing, ("letter", "burst"))
                    self.assertTrue(BURST_RANGE[0] <= step.key_factor <= LETTER_RANGE[1])

    def test_slow_unit_classification(self):
        for unit in ("@", "#", "%", "+", "=", "&", "é", "é", "😊", FAMILY, "€", " "):
            self.assertTrue(is_slow_unit(unit), unit)
        for unit in ("a", "Z", "7", " ", "\n", "\t", ".", ",", "?", "!", "'", "-"):
            self.assertFalse(is_slow_unit(unit), unit)

    def test_other_typo_kinds_are_fixed_too(self):
        cases = {
            ("the", "double"): ["t", "th", "thh", "th", "the"],
            ("The", "shift"): ["T", "TH", "T", "Th", "The"],
            ("the", "skip"): ["t", "te", "t", "th", "the"],
        }
        for (text, kind), states in cases.items():
            with self.subTest(kind=kind):
                plan = plan_typing(text, True, random.Random(0), typo_rate=1.0, typo_kinds=(kind,))
                self.assertEqual(visible_states(plan), states)

    def test_ordinary_letters_vary_and_bursts_occur(self):
        plan = plan_typing("lorem ipsum dolor sit amet consectetur " * 30, False, random.Random(6))
        letters = [s.key_factor for s in plan if s.timing == "letter"]
        bursts = [s.key_factor for s in plan if s.timing == "burst"]
        self.assertGreater(statistics.pstdev(letters), 0.15)
        self.assertGreater(len(bursts), 10)
        self.assertLessEqual(max(bursts), BURST_RANGE[1])
        self.assertLess(statistics.fmean(bursts), statistics.fmean(letters))

    def test_common_words_come_out_in_bursts(self):
        plan = plan_typing("and the of to in is it " * 60, False, random.Random(9))
        burst_share = sum(s.timing == "burst" for s in plan) / len(plan)
        self.assertGreater(burst_share, 0.4)

    def test_fluent_bigrams_are_faster_than_same_finger_pairs(self):
        rng = random.Random(10)
        fast, slow = [], []
        for _ in range(300):
            fast.append(plan_typing("xthx", False, rng)[2].key_factor)  # "th": practised pair
            slow.append(plan_typing("xunx", False, rng)[2].key_factor)  # "un": same finger
        self.assertLess(statistics.fmean(fast) * 1.2, statistics.fmean(slow))

    def test_sentence_comma_and_paragraph_pauses(self):
        plan = plan_typing("One. Two!\n\nThree\nfour, five", False, random.Random(8))
        space_after_one = plan[4]
        self.assertEqual((space_after_one.text, space_after_one.pause), (" ", "sentence"))
        first_newline = plan[9]
        self.assertEqual((first_newline.text, first_newline.pause), ("\n", "sentence"))
        three = plan[11]
        self.assertEqual((three.text, three.pause), ("T", "paragraph"))
        four = plan[17]
        self.assertEqual((four.text, four.pause), ("f", "line"))
        space_after_comma = plan[22]
        self.assertEqual((space_after_comma.text, space_after_comma.pause), (" ", "comma"))
        base = 200
        ordinary = max(s.delay_ms(base) for s in plan if not s.pause)
        self.assertGreater(space_after_one.delay_ms(base), ordinary)
        self.assertGreater(three.pause_factor, space_after_one.pause_factor)
        self.assertGreater(space_after_one.pause_factor, space_after_comma.pause_factor)


if __name__ == "__main__":
    unittest.main()
