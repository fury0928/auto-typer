# Auto-Typer

A Windows 10/11 auto-typer written in Python with Tkinter. It types the text from a
**Source** box into its own built-in **Test editor** using Win32 `SendInput`
(`KEYEVENTF_UNICODE`), at a human-like pace with optional visible typo corrections.
It never reads or writes the clipboard.

Window layout, top to bottom: Source box · Speed slider, *Show typing corrections*,
Start / Stop · Test editor · progress bar and status line.

## Requirements

- Windows 10 or Windows 11
- Python 3.8 or newer from [python.org](https://www.python.org/downloads/windows/)
  (the installer includes Tkinter). No third-party packages are needed.

## Run

```powershell
git clone https://github.com/fury0928/auto-typer.git
cd auto-typer
py autotyper.py
```

Options:

| Option | Purpose |
| --- | --- |
| `--seed N` | Repeatable runs (same pauses and typos for the same text). |
| `--timing-log run.csv` | After each run, write one row per keystroke: planned and measured wait, ratio to the base delay, timing category. |

## Using it

1. Type or paste (Ctrl+V) your text into **Source text**.
2. Set **Speed** (default 45 WPM, about an average human typist). The base delay
   is `60 000 / (WPM × 5)` ms per key, which is 267 ms at 45 WPM. You can move the
   slider during a run and the change applies from the next keystroke.
3. Leave **Show typing corrections** on (the default) for visible typos and fixes.
4. Press **Start**. The app snapshots the source text, clears and focuses the
   Test editor, and counts down for 2 seconds before it starts typing.
5. To stop, press **Esc**, click **Stop**, or move focus anywhere else (another
   window, or the source box). Whatever has already been typed stays in the
   editor.

During a run, the status line shows each keystroke's timing category, its factor
and the measured wait. When the run ends it reports whether the editor matches the
input exactly, whether the clipboard was untouched, and the measured wait ÷ base
delay for each kind of keystroke.

## How the pace works

Every wait is a multiple of the base delay set by the slider.

| Keystroke | Wait before it |
| --- | --- |
| Ordinary letters, digits, space, `. , ; : ! ? ' " - ( )` | 0.5–1.45×. Log-normal jitter, with common pairs (th, he, in…) and double letters faster, same-finger pairs slower, Shift-capitals and word starts a little slower, and a gentle drift in overall tempo |
| Fast bursts (frequent short words like *the*, *and*, *of*, plus random 3–7 key runs) | 0.35–0.6× |
| Symbols `@ # $ % ^ & * + = _ ~ \` \| \ / < > [ ] { }` and **any non-ASCII** character (é, 😊, 中, €…) | **1.5–2.5×** |
| After a comma / semicolon / colon | + 0.5–1.4× + up to 100 ms |
| After a sentence end (`. ! ? …` followed by whitespace) | + 2.5–4.5× + 80–250 ms |
| First key after a line break / a blank line (paragraph) | + 1.5–3× / + 5–8× + 250–600 ms |
| Before some long words (8+ letters) | + 0.8–2× hesitation |

A grapheme cluster counts as one unit: one wait, sent in a single `SendInput`
call. That covers precomposed `é`, `e` plus a combining accent, `👍🏽`, `👩‍👩‍👧`, `🇺🇸`
and `1️⃣`.

### Corrections

Roughly every 30–40 letters (never closer than 10 keystrokes apart), the app
makes a typo on a lowercase letter in the middle of a word. The typo stays on
screen for 2–3.5× the base delay plus 120–300 ms, then it is erased with Backspace
and the correct letters are retyped:

| Kind | Example for "the" / "The" |
| --- | --- |
| Swapped letters | `teh` → erase `eh` → type `he` |
| Neighbouring key, sometimes noticed 1–2 letters late | `tje` → erase → `he` |
| Skipped letter | `te` → erase `e` → type `he` |
| Doubled key | `thh` → erase one `h` |
| Shift held too long | `THe` → erase `H` → type `h` |

Every plan is replayed in the unit tests to prove that it ends with exactly the
input. At run time the editor is compared with the expected text before every
keystroke, and again at the end.

### Safety checks before every keystroke

- This window must be the foreground window and the Test editor must have keyboard
  focus. Otherwise the run stops. An editor `<FocusOut>` also stops it at once.
- The editor must contain exactly what has been typed so far. If something else
  changed it, the run stops. A stray click inside the editor only moves the caret,
  so the caret is put back at the end and any selection is cleared.
- If Shift, Ctrl, Alt or the Windows key is physically held, typing pauses until
  you release it, so a letter can't turn into a shortcut.
- There are no clipboard calls. The only clipboard API used is the read-only
  `GetClipboardSequenceNumber`, which proves afterwards that nothing changed it.

## Manual test checklist

Run `py autotyper.py` and work through these checks.

- [ ] **Longer symbol and Unicode pauses.** Set the slider to 30 WPM (base 400 ms)
  and type `plain words then @ # % + é 😊 👩‍👩‍👧 and back to plain words`. Watch the
  status line. Ordinary letters should show *ordinary key 0.50–1.45×* and roughly
  200–580 ms. Each of `@ # % + é 😊` should show *symbol/Unicode 1.5–2.5×* and
  600–1000 ms. The family emoji should show up as **one** key with 5 code points.
  For exact numbers, run `py autotyper.py --timing-log run.csv` and check the
  `actual_over_base` column.
- [ ] **Visible corrections.** Paste a few lowercase sentences, keep corrections on,
  and use 30–45 WPM. Wrong letters should appear, stay visible for about a
  second, get erased with Backspace and be retyped. The status line marks these
  keys *(typo)* and *Backspace*. With the toggle off, no Backspace should ever
  appear.
- [ ] **Exact final text.** Paste text with double spaces, a tab, trailing spaces,
  two blank lines, `Café`, `e` + U+0301, `👍🏽 👩‍👩‍👧 🇺🇸 1️⃣` and `中文`, then
  run it. The badge should show **✓ Exact match**, and the status line should say
  *matches the input exactly*. This is a code-point comparison, so it holds even
  where Tk draws an emoji sequence as separate glyphs.
- [ ] **Focus-change cancellation.** Start a long text, then in separate runs:
  (a) click the source box, (b) Alt+Tab to another app, (c) press Esc,
  (d) click Stop, (e) click another window during the countdown. Each run should
  stop with the matching reason, the text typed so far should stay in the editor,
  and nothing should be typed into the other app.
- [ ] **Clipboard preservation.** In Notepad, copy `CLIPBOARD-SENTINEL-123`. Run a
  full text with corrections on and stop one run halfway through. The status
  line should say *Clipboard untouched*, and pasting in Notepad should still give
  the sentinel.
- [ ] **Responsiveness.** During a run, drag the speed slider. The speed should
  change at once and typing should continue. Hovering and clicking Stop should
  respond immediately.

## Automated checks

```powershell
py -m unittest discover -s tests -v   # 15 planner tests, any OS, ~5 s
py tests\gui_selftest.py              # 39 end-to-end checks with real SendInput, ~2 min
```

The self-test opens the app and drives it with real keystrokes. Don't touch the
mouse or keyboard while it runs: the app will (correctly) treat that as a focus
change. It runs at 120–200 WPM so that it finishes quickly; this is not the app's
default speed.

## Verification log (2026-11-17)

Environment: Windows Server 2025 (build 10.0.26100, the same NT build as
Windows 11 24H2), connected over RDP, Python 3.12.10, Tk 8.6.15.

| Check | Result |
| --- | --- |
| Unit tests: grapheme splitting; 9 000 random plans reproduce their input exactly; every Backspace erases only a typo letter; `teh` → `he`; other typo kinds; corrections off; symbol and Unicode factors 1.5–2.5×; letter variation; bursts; bigram vs same-finger; sentence, comma and paragraph pauses | 15/15 pass |
| GUI self-test: exact final text (305 code points with tab, double and trailing spaces, blank lines, combining accent, ZWJ, flag, keycap and skin-tone emoji) with corrections on and off | pass |
| Measured symbol and Unicode waits | 1.68–2.50× (n = 17), ordinary 0.51–1.47×, bursts 0.37–0.59× |
| Sentence and paragraph pauses longer than any ordinary key | pass (6.4× and 13×) |
| Typos visible on screen before Backspace, then retyped | pass (3 per 305 characters) |
| Scheduling accuracy | median 0.5 ms, p95 1.2 ms late |
| UI responsiveness during typing (20 ms heartbeat) | worst gap 45 ms |
| Stop button, real Esc keystroke, focus to source box, focus lost during countdown, another app taking the foreground | all stop with the right reason, typed text kept, no keystrokes afterwards |
| Caret moved and text selected mid-run; slider moved mid-run | output still exact; speed changed live |
| Clipboard sequence number across all runs | unchanged |

Not yet verified: retail Windows 10 or 11 desktops, the manual checklist by eye,
high-DPI scaling, and non-US keyboard layouts or IMEs. Typing itself doesn't
depend on the keyboard layout; only the typo neighbours assume QWERTY.

## Limitations

- Tk 8.6 draws emoji in monochrome and may draw ZWJ sequences as several glyphs.
  The characters themselves are exact.
- The first time Tk displays a new kind of emoji, it can pause for a few hundred
  milliseconds while it looks for a fallback font. This happens when you paste,
  before typing starts.
- `SendInput` has no target window. The app checks focus right before each
  keystroke, which leaves a sub-millisecond window if focus changes at exactly
  that moment.
- A lone carriage return (`\r`) can't be reproduced in a Tk text box. If one
  appears, the run stops and says which keystroke did not appear.
