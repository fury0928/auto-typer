"""Auto-typer for Windows 10 and 11.

Types the Source text into the built-in Test editor with Win32 SendInput, at a
human-like pace with optional visible typo corrections. The clipboard is never
read or written.

    py autotyper.py [--seed N] [--timing-log run.csv]

Everything runs on the Tk UI thread: each keystroke is sent from an after()
timer callback, so the window keeps repainting and reacting to Stop and Escape
between keystrokes. Before every keystroke the run checks that this window is
in the foreground, that the Test editor has keyboard focus, and that the editor
still holds exactly what has been typed so far.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import statistics
import sys
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

from typing_plan import BACKSPACE, Step, plan_typing

if sys.platform != "win32":
    sys.exit("autotyper.py needs Windows: it types with the Win32 SendInput API.")

import win_input as wi  # noqa: E402  (Windows-only module)

COUNTDOWN_S = 2.0
SYNC_TIMEOUT_S = 0.75   # how long a keystroke may take to appear in the editor
POLL_MS = 4
WPM_MIN, WPM_MAX, WPM_DEFAULT = 15, 200, 45  # 45 WPM ≈ an average human typist

ACCENT = "#0067c0"
SUCCESS = "#0f7b0f"
WARNING = "#9d5d00"
MUTED = "#5c5c5c"
BORDER = "#c4c4c4"
TEXT_FONT = ("Consolas", 11)

TIMING_NAMES = {
    "letter": "ordinary key",
    "burst": "fast burst",
    "slow": "symbol/Unicode",
    "backspace": "Backspace",
}
PAUSE_NAMES = {
    "sentence": "sentence-end pause",
    "paragraph": "paragraph pause",
    "line": "line-break pause",
    "comma": "comma pause",
    "word": "hesitation before a long word",
    "notice": "typo noticed",
    "resume": "resume after fix",
}


@dataclass
class TimingRecord:
    index: int
    step: Step
    base_ms: float      # base delay in force when this step was scheduled
    planned_ms: float   # planned wait since the previous keystroke
    actual_ms: float    # measured wait since the previous keystroke
    held: bool          # held back (editor catching up, or modifier keys down)


def describe_key(step: Step) -> str:
    if step.action == BACKSPACE:
        return "Backspace"
    names = {"\n": "Enter", "\t": "Tab", " ": "Space"}
    if step.text in names:
        return names[step.text]
    if step.text.isascii():
        return f"“{step.text}”"
    return f"“{step.text}” ({' '.join(f'U+{ord(c):04X}' for c in step.text)})"


def format_duration(ms: float) -> str:
    seconds = max(0, round(ms / 1000))
    return f"{seconds // 60}:{seconds % 60:02d}"


def timing_summary(records: list[TimingRecord]) -> str:
    """Measured wait / base delay for each kind of keystroke in a run."""
    groups: dict[str, list[float]] = {
        "ordinary": [], "bursts": [], "symbols & Unicode": [],
        "after sentences": [], "after paragraphs": [], "typo pauses": [],
    }
    for rec in records:
        if rec.held:
            continue
        step, ratio = rec.step, rec.actual_ms / rec.base_ms
        key = {"sentence": "after sentences", "paragraph": "after paragraphs",
               "notice": "typo pauses"}.get(step.pause)
        if key is None and not step.pause:
            key = {"letter": "ordinary", "burst": "bursts", "slow": "symbols & Unicode"}.get(step.timing)
        if key:
            groups[key].append(ratio)
    parts = []
    for name, vals in groups.items():
        if vals:
            spread = f" ({min(vals):.2f}–{max(vals):.2f})" if name in ("ordinary", "symbols & Unicode") else ""
            parts.append(f"{name} {statistics.fmean(vals):.2f}×{spread}")
    return "Measured wait ÷ base delay: " + " · ".join(parts) if parts else ""


class TypingRun:
    """One Start → finished/stopped run, driven by Tk timers on the UI thread."""

    def __init__(self, app: AutoTyperApp, target: str, steps: list[Step]):
        self.app = app
        self.target = target
        self.steps = steps
        self.state = "countdown"   # countdown → typing → finishing → done | stopped
        self.index = 0             # next step to send
        self.expected = ""         # what the editor must contain right now
        self.after_id: str | None = None
        self.countdown_end = 0.0
        self.due = 0.0             # perf_counter() time the next step is due
        self.last_sent = 0.0
        self.planned_ms = 0.0
        self.planned_base = 0.0
        self.held = False
        self.sync_since: float | None = None
        self.started = 0.0
        self.finished = 0.0
        self.records: list[TimingRecord] = []
        self.correction_snapshots: list[str] = []  # editor text while each typo is on screen
        self.corrections = sum(1 for s in steps if s.pause == "notice")
        self.outcome = ""
        self.success = False
        self.clipboard_seq = wi.clipboard_sequence_number()
        self.clipboard_unchanged = True
        self._timer_period = False
        # Suffix sums for the time-remaining estimate (works for any base delay).
        self._rest_factor = [0.0] * (len(steps) + 1)
        self._rest_ms = [0.0] * (len(steps) + 1)
        for i in range(len(steps) - 1, -1, -1):
            self._rest_factor[i] = self._rest_factor[i + 1] + steps[i].key_factor + steps[i].pause_factor
            self._rest_ms[i] = self._rest_ms[i + 1] + steps[i].pause_ms

    @property
    def active(self) -> bool:
        return self.state in ("countdown", "typing", "finishing")

    @property
    def sending(self) -> bool:
        return self.state in ("countdown", "typing")

    def remaining_ms(self, base_ms: float) -> float:
        return self._rest_factor[self.index] * base_ms + self._rest_ms[self.index]

    # Scheduling ----------------------------------------------------------------

    def start(self) -> None:
        self._timer_period = wi.begin_timer_resolution()
        self.countdown_end = time.perf_counter() + COUNTDOWN_S
        self.app.show_countdown(math.ceil(COUNTDOWN_S))
        self._later(1, self._countdown_tick)

    def _later(self, ms: float, callback) -> None:
        self.after_id = self.app.root.after(max(1, int(ms)), callback)

    def _countdown_tick(self) -> None:
        self.after_id = None
        if self.state != "countdown":
            return
        problem = self.app.focus_problem()
        if problem:
            self.stop(problem)
            return
        now = time.perf_counter()
        remaining = self.countdown_end - now
        if remaining > 0:
            self.app.show_countdown(math.ceil(remaining))
            self._later(min(100.0, remaining * 1000), self._countdown_tick)
            return
        self.state = "typing"
        self.started = self.last_sent = now
        self._schedule_next(now)
        self.app.on_typing_started(self)
        self._tick()

    def _schedule_next(self, sent: float) -> None:
        self.planned_base = self.app.base_delay_ms()
        self.planned_ms = self.steps[self.index].delay_ms(self.planned_base)
        self.due = sent + self.planned_ms / 1000

    def _tick(self) -> None:
        """Send the next keystroke once it is due and it is safe to do so."""
        self.after_id = None
        if self.state != "typing":
            return
        problem = self.app.focus_problem()
        if problem:
            self.stop(problem)
            return
        if wi.key_down(wi.VK_ESCAPE):
            self.stop("Escape was pressed")
            return
        now = time.perf_counter()
        if now < self.due - 0.0005:
            self._later((self.due - now) * 1000, self._tick)
            return
        if not self._editor_in_sync(now, self._tick):
            return
        held = wi.held_modifiers()
        if held:
            # A held Ctrl/Alt/Shift/Windows key would turn the keystroke into a shortcut.
            self.held = True
            self.app.show_hold(held)
            self._later(25, self._tick)
            return
        editor = self.app.editor
        editor.tag_remove("sel", "1.0", "end")  # a stray click must not redirect typing
        editor.mark_set("insert", "end-1c")
        step = self.steps[self.index]
        if step.pause == "notice":
            self.correction_snapshots.append(self.expected)
        try:
            if step.action == BACKSPACE:
                wi.press_backspace()
            else:
                wi.type_text(step.text)
        except OSError as exc:
            self.stop(f"Windows rejected the keystroke ({exc.strerror or exc})")
            return
        sent = time.perf_counter()
        record = TimingRecord(self.index, step, self.planned_base, self.planned_ms,
                              (sent - self.last_sent) * 1000, self.held)
        self.records.append(record)
        self.expected = self.expected[:-1] if step.action == BACKSPACE else self.expected + step.text
        self.last_sent = sent
        self.held = False
        self.index += 1
        self.app.on_step(self, record)
        if self.index == len(self.steps):
            self.state = "finishing"
            self._finish()
            return
        self._schedule_next(sent)
        self._later(self.planned_ms, self._tick)

    def _editor_in_sync(self, now: float, retry) -> bool:
        """True if the editor shows exactly what was typed; otherwise wait briefly or stop."""
        actual = self.app.editor.get("1.0", "end-1c")
        if actual == self.expected:
            self.sync_since = None
            return True
        if self.sync_since is None:
            self.sync_since = now
        if now - self.sync_since <= SYNC_TIMEOUT_S:
            self.held = True
            self._later(POLL_MS, retry)  # the last keystroke may still be in the input queue
            return False
        last = self.records[-1].step if self.records else None
        if last and last.action != BACKSPACE and actual + last.text == self.expected:
            self.stop(f"the keystroke {describe_key(last)} did not appear in the test editor")
        else:
            self.stop("the test editor was changed by something other than the typer")
        return False

    def _finish(self) -> None:
        self.after_id = None
        if self.state != "finishing":
            return
        if not self._editor_in_sync(time.perf_counter(), self._finish):
            return
        self.success = self.expected == self.target
        self.finished = time.perf_counter()
        elapsed = format_duration((self.finished - self.started) * 1000)
        if self.success:
            fixes = f", {self.corrections} correction{'s' * (self.corrections != 1)}"
            self._end("done", f"Done in {elapsed}: the test editor matches the input exactly "
                              f"({len(self.target)} characters{fixes}).")
        else:
            self._end("done", "Finished, but the test editor does not match the input.")

    def stop(self, reason: str) -> None:
        if not self.active:
            return
        if self.state == "countdown":
            message = f"Stopped before typing started: {reason}."
        else:
            message = f"Stopped: {reason}. The text typed so far is left in the test editor."
        self._end("stopped", message)

    def _end(self, state: str, message: str) -> None:
        if self.after_id is not None:
            self.app.root.after_cancel(self.after_id)
            self.after_id = None
        wi.end_timer_resolution(self._timer_period)
        self._timer_period = False
        self.state = state
        self.outcome = message
        self.clipboard_unchanged = wi.clipboard_sequence_number() == self.clipboard_seq
        self.app.on_run_ended(self)


class AutoTyperApp:
    def __init__(self, root: tk.Tk, seed: int | None = None, timing_log: str | None = None):
        self.root = root
        self.rng = random.Random(seed)
        self.timing_log = timing_log
        self.run: TypingRun | None = None
        self.toplevel_hwnd = 0
        self.run_ended_hook = None  # optional callable(run), used by the self-test
        self.wpm_var = tk.DoubleVar(value=WPM_DEFAULT)
        self.corrections_var = tk.BooleanVar(value=True)
        self.speed_text = tk.StringVar()
        self.badge_text = tk.StringVar()
        self.status_text = tk.StringVar(value="Ready. Type or paste text above, then press Start.")
        self.detail_text = tk.StringVar()
        self._build_ui()
        self._on_speed()
        root.bind_all("<Escape>", self._on_escape, add="+")
        self.editor.bind("<FocusOut>", self._on_editor_focus_out, add="+")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.source.focus_set()

    # UI ------------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root
        root.title("Auto-Typer")
        root.geometry("920x720")
        root.minsize(780, 540)
        for name in ("TkDefaultFont", "TkTextFont"):
            tkfont.nametofont(name).configure(family="Segoe UI", size=10)
        style = ttk.Style(root)
        style.configure("Heading.TLabel", font=("Segoe UI Semibold", 11))
        style.configure("Hint.TLabel", foreground=MUTED)
        style.configure("Badge.TLabel", font=("Segoe UI Semibold", 11))
        style.configure("Detail.TLabel", foreground=MUTED, font=("Segoe UI", 9))

        outer = ttk.Frame(root, padding=(16, 12, 16, 12))
        outer.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        outer.rowconfigure(4, weight=1)

        ttk.Label(outer, text="Source text", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="Type here or paste with Ctrl+V. Start takes a snapshot.",
                  style="Hint.TLabel").grid(row=0, column=0, sticky="e")
        self.source = self._text_box(outer, row=1, undo=True)

        controls = ttk.Frame(outer)
        controls.grid(row=2, column=0, sticky="ew", pady=12)
        controls.columnconfigure(3, weight=1)
        ttk.Label(controls, text="Speed").grid(row=0, column=0, padx=(0, 8))
        self.speed_scale = ttk.Scale(controls, from_=WPM_MIN, to=WPM_MAX, length=200,
                                     variable=self.wpm_var, command=self._on_speed)
        self.speed_scale.grid(row=0, column=1)
        ttk.Label(controls, textvariable=self.speed_text, width=19).grid(row=0, column=2, padx=(8, 16))
        self.corrections_check = ttk.Checkbutton(controls, text="Show typing corrections",
                                                 variable=self.corrections_var)
        self.corrections_check.grid(row=0, column=3, sticky="w")
        self.start_button = ttk.Button(controls, text="Start", width=10, command=self.start)
        self.start_button.grid(row=0, column=4, padx=(8, 6))
        # takefocus=False: clicking Stop must not pull focus out of the editor first.
        self.stop_button = ttk.Button(controls, text="Stop", width=10, takefocus=False,
                                      command=lambda: self.stop("the Stop button was clicked"))
        self.stop_button.grid(row=0, column=5)
        self.stop_button.state(["disabled"])

        ttk.Label(outer, text="Test editor", style="Heading.TLabel").grid(row=3, column=0, sticky="w")
        self.badge = ttk.Label(outer, textvariable=self.badge_text, style="Badge.TLabel")
        self.badge.grid(row=3, column=0, sticky="e")
        self.editor = self._text_box(outer, row=4, undo=False)

        status = ttk.Frame(outer)
        status.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        status.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(status, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status_label = ttk.Label(status, textvariable=self.status_text)
        self.status_label.grid(row=1, column=0, sticky="nw", pady=(6, 0))
        self.detail_label = ttk.Label(status, textvariable=self.detail_text, style="Detail.TLabel")
        self.detail_label.grid(row=2, column=0, sticky="nw", pady=(2, 0))

        def wrap(event):
            for label in (self.status_label, self.detail_label):
                label.configure(wraplength=max(200, event.width - 8))
        status.bind("<Configure>", wrap)
        # Fixed height (two lines per label): status text changing on every keystroke
        # must never re-layout and redraw the text boxes, or keystrokes would run late.
        root.update_idletasks()
        lines = (2 * tkfont.nametofont("TkDefaultFont").metrics("linespace")
                 + 2 * tkfont.Font(font=style.lookup("Detail.TLabel", "font")).metrics("linespace"))
        status.configure(height=self.progress.winfo_reqheight() + lines + 12)
        status.grid_propagate(False)

    def _text_box(self, parent: ttk.Frame, row: int, undo: bool) -> tk.Text:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="nsew", pady=(4, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = tk.Text(frame, wrap="word", undo=undo, height=8, font=TEXT_FONT, padx=8, pady=6,
                       relief="flat", borderwidth=0, highlightthickness=2,
                       highlightbackground=BORDER, highlightcolor=ACCENT)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        return text

    @property
    def wpm(self) -> int:
        return int(round(self.wpm_var.get()))

    def base_delay_ms(self) -> float:
        """Ordinary per-key delay: words per minute with the usual 5 characters per word."""
        return 60_000 / (self.wpm * 5)

    def _on_speed(self, _value=None) -> None:
        self.wpm_var.set(self.wpm)  # snap to whole WPM
        self.speed_text.set(f"{self.wpm} WPM · {self.base_delay_ms():.0f} ms/key")

    def _set_badge(self, text: str, color: str = ACCENT) -> None:
        self.badge_text.set(text)
        self.badge.configure(foreground=color)

    # Focus ---------------------------------------------------------------------

    def focus_problem(self) -> str | None:
        """Why it is not safe to send a keystroke right now (None if it is safe)."""
        if wi.foreground_window() != self.toplevel_hwnd:
            return "another window became active"
        try:
            focused = self.root.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is not self.editor:
            return "keyboard focus left the test editor"
        return None

    def _on_editor_focus_out(self, _event) -> None:
        if self.run and self.run.sending:
            self.root.after_idle(self._recheck_focus)

    def _recheck_focus(self) -> None:
        if self.run and self.run.sending:
            problem = self.focus_problem()
            if problem:
                self.run.stop(problem)

    # Run control ---------------------------------------------------------------

    def start(self) -> None:
        if self.run and self.run.active:
            return
        text = self.source.get("1.0", "end-1c")  # the snapshot; later edits don't matter
        if not text:
            self.status_text.set("Nothing to type: enter or paste text in the source box first.")
            self.source.focus_set()
            return
        steps = plan_typing(text, corrections=self.corrections_var.get(), rng=self.rng)
        self.editor.delete("1.0", "end")
        self.editor.focus_set()
        self.toplevel_hwnd = wi.toplevel_window(self.root.winfo_id())
        self.run = TypingRun(self, text, steps)
        self.start_button.state(["disabled"])
        self.corrections_check.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.progress.configure(maximum=len(steps), value=0)
        self.status_text.set("Get ready: typing starts after the countdown. Esc or Stop cancels.")
        self.detail_text.set(f"{len(steps)} keystrokes planned, about "
                             f"{format_duration(self.run.remaining_ms(self.base_delay_ms()))} at this speed.")
        self.run.start()

    def stop(self, reason: str) -> None:
        if self.run and self.run.active:
            self.run.stop(reason)

    def _on_escape(self, _event=None):
        if self.run and self.run.active:
            self.run.stop("Escape was pressed")
            return "break"
        return None

    def _on_close(self) -> None:
        self.stop("the window was closed")
        self.root.destroy()

    # Run callbacks -------------------------------------------------------------

    def show_countdown(self, seconds: int) -> None:
        self._set_badge(f"Typing starts in {seconds}…")

    def show_hold(self, keys: list[str]) -> None:
        self.status_text.set(f"Paused: release {' + '.join(keys)} to continue typing.")

    def on_typing_started(self, run: TypingRun) -> None:
        self._set_badge("● Typing (Esc to stop)")

    def on_step(self, run: TypingRun, rec: TimingRecord) -> None:
        step = rec.step
        self.progress.configure(value=run.index)
        percent = 100 * run.index // len(run.steps)
        self.status_text.set(f"Typing… {percent}% · about {format_duration(run.remaining_ms(self.base_delay_ms()))} "
                             f"left · Esc or Stop cancels")
        detail = f"Last key {describe_key(step)}: {TIMING_NAMES[step.timing]} {step.key_factor:.2f}×"
        if step.pause:
            detail += f" + {PAUSE_NAMES[step.pause]} {step.pause_factor:.1f}× + {step.pause_ms:.0f} ms"
        if step.typo:
            detail += " (typo)"
        detail += (f" · waited {rec.actual_ms:.0f} ms (planned {rec.planned_ms:.0f} ms, "
                   f"base {rec.base_ms:.0f} ms)")
        self.detail_text.set(detail)

    def on_run_ended(self, run: TypingRun) -> None:
        self.start_button.state(["!disabled"])
        self.corrections_check.state(["!disabled"])
        self.stop_button.state(["disabled"])
        clipboard = ("Clipboard untouched." if run.clipboard_unchanged else
                     "Note: the clipboard changed during the run (this app never writes to it).")
        self.status_text.set(f"{run.outcome} {clipboard}")
        if run.state == "done" and run.success:
            self._set_badge("✓ Exact match", SUCCESS)
        elif run.state == "done":
            self._set_badge("✗ Mismatch", WARNING)
        else:
            self._set_badge("■ Stopped", WARNING)
        self.detail_text.set(timing_summary(run.records))
        if self.timing_log and run.records:
            self._write_timing_log(run)
        if self.run_ended_hook:
            self.run_ended_hook(run)

    def _write_timing_log(self, run: TypingRun) -> None:
        try:
            with open(self.timing_log, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(["step", "key", "code_points", "timing", "key_factor", "pause",
                                 "pause_factor", "pause_ms", "typo", "base_ms", "planned_ms",
                                 "actual_ms", "actual_over_base", "held"])
                for rec in run.records:
                    s = rec.step
                    writer.writerow([
                        rec.index, describe_key(s), " ".join(f"U+{ord(c):04X}" for c in s.text),
                        s.timing, f"{s.key_factor:.3f}", s.pause, f"{s.pause_factor:.3f}",
                        f"{s.pause_ms:.0f}", int(s.typo), f"{rec.base_ms:.1f}", f"{rec.planned_ms:.1f}",
                        f"{rec.actual_ms:.1f}", f"{rec.actual_ms / rec.base_ms:.3f}", int(rec.held),
                    ])
        except OSError as exc:
            self.detail_text.set(f"Could not write the timing log: {exc}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Human-paced auto-typer with a built-in test editor.")
    parser.add_argument("--seed", type=int, help="random seed, for repeatable runs")
    parser.add_argument("--timing-log", metavar="CSV", help="write per-keystroke timings here after each run")
    args = parser.parse_args(argv)
    wi.enable_dpi_awareness()
    root = tk.Tk()
    AutoTyperApp(root, seed=args.seed, timing_log=args.timing_log)
    root.mainloop()


if __name__ == "__main__":
    main()
