# Typing Tester (auto-typer)

A Windows 10/11 auto-typer written in Python with Tkinter. It types the text in its
**Source** box like a person copy-typing it. It types into either its own
**Test editor** or a text box in another window, such as a browser text field or
Notepad, chosen with **Pick target…**. Keystrokes go through Win32 `SendInput`
(`KEYEVENTF_UNICODE`), and the clipboard is never read or written.

Window layout, top to bottom:

- Source box
- Speed slider, *Show typing corrections*, Start / Stop
- Type into: target, Pick target… / Use test editor
- Test editor
- Progress bar and status line

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

| Option | Purpose |
| --- | --- |
| `--seed N` | Repeatable runs (same pauses and typos for the same text). |
| `--timing-log run.csv` | After each run, write one row per keystroke: planned and measured wait, ratio to the base delay, timing category. |

## Using it

1. Type or paste (Ctrl+V) your text into **Source text**.
2. Set **Speed**. It is the *overall* typing speed, including every pause and
   correction, so 40 WPM (the default, about an average adult) really finishes
   at 40 WPM. Keys inside words therefore come faster than the average, about
   160 ms apart at 40 WPM, just as with a real typist. You can move the slider
   during a run and the change applies from the next keystroke.
3. Leave **Show typing corrections** on (the default) for visible typos and fixes.
4. Choose where to type:
   - **Test editor** (the default). Start clears it, and the result is verified
     after every keystroke.
   - **Pick target…**, then click inside the text box you want in another window
     (a browser field, Notepad, a chat box…). The app shows the window it
     picked. Come back and press Start. **Use test editor** switches back.
5. Press **Start**. The app snapshots the source text and counts down for
   2 seconds, then types. With an external target it first brings that window
   to the front.
6. To stop, press **Esc** (this works in any window), click **Stop**, or move
   focus anywhere else. With an external target, any mouse click also stops
   it. Whatever has already been typed stays where it is.

During a run, the status line shows each keystroke's timing category, factor and
measured wait. When the run ends it reports the result, whether the clipboard was
untouched, and the measured wait ÷ base delay for each kind of keystroke.

## How the pace works

The *base delay* is the ordinary gap between two keystrokes inside a word. The
app works it out from the slider so the whole run averages the chosen WPM
(5 characters per word). Every other wait is a multiple of it:

| Keystroke | Wait before it |
| --- | --- |
| Ordinary letters, digits, space, `. , ; : ! ? ' " - ( )` | 0.5–1.45×. Log-normal jitter, with a slower warm-up for the first ~20 keys; common pairs (th, he, in…) and double letters faster; same-finger pairs, rare letters (q z x j k v), digits and Shift-capitals slower; a quick thumb on the space bar; and a gently drifting tempo |
| Fast bursts (frequent short words like *the*, *and*, *of*, plus random 3–7 key runs) | 0.35–0.6× |
| Symbols `@ # $ % ^ & * + = _ ~ \` \| \ / < > [ ] { }` and **any non-ASCII** character (é, 😊, 中, €…) | **1.5–2.5×** |
| Every 3–7 words: a glance back at the source | + 0.8–2× + 150–450 ms |
| Before some long words (8+ letters) | + 0.8–2× hesitation |
| Momentary stall on a letter (3%) | + 0.3–1× |
| Rarely (~1 word in 170): losing your place | + 3–6× + 0.6–1.5 s |
| After a comma / semicolon / colon | + 0.5–1.4× + up to 100 ms |
| After a sentence end (`. ! ? …` followed by whitespace) | + 2.5–4.5× + 80–250 ms |
| First key after a line break / a blank line (paragraph) | + 1.5–3× / + 5–8× + 250–600 ms |

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
| Neighbouring key, sometimes noticed 1–2 letters late | `tje` → erase → `he` |
| Swapped letters | `teh` → erase `eh` → type `he` |
| Skipped letter | `te` → erase `e` → type `he` |
| Doubled key | `thh` → erase one `h` |
| Shift held too long | `THe` → erase `H` → type `h` |

Every plan is replayed in the unit tests to prove that it ends with exactly the
input.

## Safety checks

Before every keystroke, the app checks the following and stops the run if any of
them fail:

- **Test editor:** this window must be in front and the editor must have focus.
  The editor must also contain exactly what has been typed so far. A stray
  click inside the editor only moves the caret, so the caret is put back at the
  end and any selection is cleared.
- **External target:** the picked window must still exist and be in front, and
  the same control must have keyboard focus. Escape and mouse buttons are polled
  every 10 ms. If the text contains line breaks or tabs, Start asks first,
  because in another app Enter can submit a single-line form and Tab jumps to
  the next field.
- **Both:** if Shift, Ctrl, Alt or the Windows key is physically held, typing
  pauses until you release it, so a letter can't turn into a shortcut.
- **Clipboard:** there are no clipboard calls. The only clipboard API used is the
  read-only `GetClipboardSequenceNumber`, which proves afterwards that nothing
  changed it.

### Browser text boxes

Typing into a browser field works like typing on the keyboard. Two limits come
from how browsers work:

- A browser draws its own text fields, so Windows can see the browser window
  and its page area but not which field inside the page has focus. The app
  restores the browser window and trusts the browser to put focus back in the
  field you clicked. A mouse click during typing stops the run, because a click
  is how focus normally moves to another field.
- The typed text can't be read back from a browser. At the end, the app says
  *check it by eye*. For classic Edit and RichEdit controls (classic Notepad,
  most dialog boxes) it reads the text back and confirms an exact match.

The app also can't type into programs running as administrator unless it runs as
administrator too (a Windows UIPI rule).

## Manual test checklist

Run `py autotyper.py` and work through these checks.

- [ ] **Longer symbol and Unicode pauses.** Set the slider to 20 WPM and type
  `plain words then @ # % + é 😊 👩‍👩‍👧 and back to plain words`. In the status line,
  ordinary letters should show *ordinary key 0.50–1.45×*. Each of `@ # % + é 😊`
  should show *symbol/Unicode 1.5–2.5×*, with a measured wait that matches that
  factor times the shown base. The family emoji should show up as **one** key
  with 5 code points. For exact numbers, run with `--timing-log run.csv` and
  check `actual_over_base`.
- [ ] **Visible corrections.** Paste a few lowercase sentences, keep corrections on,
  and use 30–40 WPM. Wrong letters should appear, stay visible for about a
  second, get erased with Backspace and be retyped. The status line marks these
  keys *(typo)* and *Backspace*. With the toggle off, no Backspace should ever
  appear.
- [ ] **Exact final text.** Paste text with double spaces, a tab, trailing spaces,
  two blank lines, `Café`, `e` + U+0301, `👍🏽 👩‍👩‍👧 🇺🇸 1️⃣` and `中文`, then run it in
  the Test editor. The badge should show **✓ Exact match**.
- [ ] **External target, Notepad.** Pick target → click into Notepad → come back →
  Start. The prompt about line breaks and tabs should appear. Afterwards the
  status line should say *contains the exact input*.
- [ ] **External target, browser.** Pick target → click into a browser text field
  (for example a search box or a `<textarea>`) → come back → Start. The browser
  should come to the front and the text should appear in that field. Clicking
  the mouse during typing should stop it.
- [ ] **Focus-change cancellation.** Start a long text, then in separate runs:
  (a) click the source box (editor mode), (b) Alt+Tab to another app,
  (c) press Esc, (d) click Stop, (e) click another window during the countdown.
  Each run should stop with the matching reason, the text typed so far should
  stay, and nothing should be typed into the wrong app.
- [ ] **Clipboard preservation.** In Notepad, copy `CLIPBOARD-SENTINEL-123`. Run a
  full text, stopping one run halfway through. The status line should say
  *Clipboard untouched*, and pasting should still give the sentinel.
- [ ] **Overall speed.** At 40 WPM, a 400-character text should take about
  2 minutes (400 ÷ 5 ÷ 40 = 2.0 minutes). The status line shows the estimate
  before typing starts.

## Automated checks

```powershell
py -m unittest discover -s tests -v   # 19 planner tests, any OS, ~6 s
py tests\gui_selftest.py              # end-to-end with real SendInput + classic Notepad, ~2.5 min
```

The self-test needs an unlocked desktop in front (not a minimized remote-desktop
window). Don't touch the mouse or keyboard while it runs. It types at
80–150 WPM so that it finishes quickly.

## Verification log (2026-11-17)

Environment: Windows Server 2025 (build 10.0.26100, the same NT build as
Windows 11 24H2), over RDP, Python 3.12.10, Tk 8.6.15.

| Check | Result |
| --- | --- |
| Unit tests (19): grapheme splitting; 9 000 random plans reproduce their input; Backspace only erases typo letters; all five typo kinds; overall-WPM calibration; paragraph pause never replaced; copy-typing pauses; warm-up; symbol and Unicode 1.5–2.5×; bursts; bigrams; sentence, comma and paragraph pauses | 19/19 pass |
| GUI self-test, run before the target picker and overall-WPM changes: exact text with corrections on/off; symbol and Unicode waits 1.68–2.50×; typos visible; p95 scheduling error 1.2 ms; worst UI stall 45 ms; Stop, Esc, focus loss, countdown focus loss and another app taking the foreground all stop with text kept; clipboard unchanged | 39/39 pass |
| GUI self-test after adding the target picker | **Not run.** The session had no foreground window (remote desktop minimized or locked), so the harness correctly skipped. |
| Smoke test of the new code with no foreground window: a run starts and stops cleanly during the countdown; picking starts and cancels; a real other window (VS Code) works as a `WindowTarget` | pass |

Not yet verified: the new external-target scenarios in `tests/gui_selftest.py`
(Notepad read-back, held Esc, minimizing the target), a real browser target,
retail Windows 10 or 11, high-DPI scaling, and non-US keyboard layouts or IMEs.

## Limitations

- Tk 8.6 draws emoji in monochrome and may draw ZWJ sequences as several glyphs.
  The characters themselves are exact.
- The first time Tk displays a new kind of emoji, it can pause for a few hundred
  milliseconds while it looks for a fallback font. This happens when you paste,
  before typing starts.
- `SendInput` has no target window. The app re-checks focus right before each
  keystroke, which leaves a sub-millisecond window if focus changes at exactly
  that moment.
- A lone carriage return (`\r`) can't be reproduced in the Test editor. If one
  appears, the run stops and says which keystroke did not appear.
- Typo neighbours assume a QWERTY layout. Typing itself doesn't depend on the
  layout.
