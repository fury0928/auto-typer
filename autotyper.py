"""Auto-typer for Windows 10 and 11.

Types the Source text with Win32 SendInput at a human-like pace, with optional
visible typo corrections, into either the built-in Test editor or a text box in
another window (a browser text field, Notepad, ...) chosen with "Pick target".
The clipboard is never read or written.

    py autotyper.py [--seed N] [--timing-log run.csv]

Everything runs on the Tk UI thread: each keystroke is sent from an after()
timer callback, so the window keeps repainting and reacting to Stop and Escape
between keystrokes. Right before every keystroke the run checks that the target
window is in the foreground and its text box has keyboard focus; for the Test
editor it also checks that the editor holds exactly what was typed so far.
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
from tkinter import messagebox, ttk

from typing_plan import BACKSPACE, Pace, Step, plan_typing

if sys.platform != "win32":
    sys.exit("autotyper.py needs Windows: it types with the Win32 SendInput API.")

import win_input as wi  # noqa: E402  (Windows-only module)

COUNTDOWN_S = 2.0
SYNC_TIMEOUT_S = 0.75     # how long a keystroke may take to appear in the Test editor
POLL_MS = 4
WATCH_MS = 10             # how often Escape, mouse clicks and focus are polled
ACTIVATE_SETTLE_S = 0.25  # let another app finish activating before the first key
READBACK_TIMEOUT_S = 1.5  # wait for another app to process the last keystrokes
PICK_TIMEOUT_S = 20
PICK_SETTLE_S = 0.6       # the picked window must stay in front this long
WPM_MIN, WPM_MAX, WPM_DEFAULT = 10, 150, 40  # overall speed; ~40 WPM is an average adult

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
    "glance": "glance at the source",
    "think": "longer pause",
    "stall": "momentary stall",
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
    names = {"\n": "Enter", "\t": "Tab", " ": "Space", "\r\n": "Enter"}
    if step.text in names:
        return names[step.text]
    if step.text.isascii():
        return f"“{step.text}”"
    return f"“{step.text}” ({' '.join(f'U+{ord(c):04X}' for c in step.text)})"


def format_duration(ms: float) -> str:
    seconds = max(0, round(ms / 1000))
    return f"{seconds // 60}:{seconds % 60:02d}"


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


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


# --- typing targets -------------------------------------------------------------

class EditorTarget:
    """The built-in Test editor: cleared on Start and verified after every keystroke."""

    external = False

    def __init__(self, app: AutoTyperApp):
        self.app = app

    def describe(self) -> str:
        return "Test editor (built-in)"

    def name(self) -> str:
        return "the test editor"

    def prepare(self) -> None:
        self.app.editor.delete("1.0", "end")
        self.app.editor.focus_set()

    def countdown_problem(self) -> str | None:
        return self.focus_problem()

    def activate(self) -> None:
        pass

    def focus_problem(self) -> str | None:
        app = self.app
        if wi.foreground_window() != app.own_hwnd():
            return "another window became active"
        try:
            focused = app.root.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is not app.editor:
            return "keyboard focus left the test editor"
        return None

    def before_keystroke(self) -> None:
        editor = self.app.editor
        editor.tag_remove("sel", "1.0", "end")  # a stray click must not redirect typing
        editor.mark_set("insert", "end-1c")

    def read_text(self) -> str | None:
        return self.app.editor.get("1.0", "end-1c")


class WindowTarget:
    """A text box in another application's window, chosen with Pick target.

    Its existing text is never cleared. Foreground window and focused control
    are checked before every keystroke; inside a browser page Windows can't see
    which field has focus, so any mouse click during typing also stops the run.
    """

    external = True

    def __init__(self, hwnd: int, focus: int):
        self.hwnd = hwnd
        self.focus = focus
        _, pid = wi.window_thread_process(hwnd)
        self.title = wi.window_title(hwnd) or "(untitled)"
        self.exe = wi.process_name(pid)
        self.control = wi.class_name(focus) if focus else ""

    def describe(self) -> str:
        title = self.title if len(self.title) <= 60 else self.title[:57] + "…"
        return f"“{title}” · {self.exe}"

    def name(self) -> str:
        return self.exe

    def prepare(self) -> None:
        pass

    def countdown_problem(self) -> str | None:
        return None if wi.is_window(self.hwnd) else "the target window was closed"

    def activate(self) -> None:
        wi.activate_window(self.hwnd, self.focus)

    def focus_problem(self) -> str | None:
        if not wi.is_window(self.hwnd):
            return "the target window was closed"
        if wi.toplevel_window(wi.foreground_window()) != self.hwnd:
            return "another window became active"
        if self.focus and wi.focused_control() != self.focus:
            return "keyboard focus left the target text box"
        return None

    def before_keystroke(self) -> None:
        pass

    def read_text(self) -> str | None:
        return wi.read_control_text(self.focus)


# --- one run --------------------------------------------------------------------

class TypingRun:
    """One Start → finished/stopped run, driven by Tk timers on the UI thread."""

    def __init__(self, app: AutoTyperApp, target, text: str, steps: list[Step]):
        self.app = app
        self.target = target
        self.text = text           # the snapshot of the source box
        self.steps = steps
        self.pace = Pace.of(steps)
        self.state = "countdown"   # countdown → typing → finishing → done | stopped
        self.index = 0             # next step to send
        self.expected = ""         # what has been typed so far (after Backspaces)
        self.after_id: str | None = None
        self.watch_id: str | None = None
        self.countdown_end = 0.0
        self.settled_at = 0.0      # focus checks start once the target is active
        self.due = 0.0             # perf_counter() time the next step is due
        self.last_sent = 0.0
        self.planned_ms = 0.0
        self.planned_base = 0.0
        self.held = False
        self.sync_since: float | None = None
        self.started = 0.0
        self.finished = 0.0
        self.records: list[TimingRecord] = []
        self.correction_snapshots: list[str] = []  # text on screen while each typo is shown
        self.corrections = sum(1 for s in steps if s.pause == "notice")
        self.outcome = ""
        self.success: bool | None = False  # None: finished, but the target can't be read back
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

    def base_delay_ms(self) -> float:
        """Base (ordinary-key) delay that makes this whole run average the slider's WPM."""
        return self.pace.base_ms(self.app.wpm)

    def remaining_ms(self) -> float:
        return self._rest_factor[self.index] * self.base_delay_ms() + self._rest_ms[self.index]

    # Scheduling ----------------------------------------------------------------

    def start(self) -> None:
        self._timer_period = wi.begin_timer_resolution()
        self.countdown_end = time.perf_counter() + COUNTDOWN_S
        self.app.show_countdown(math.ceil(COUNTDOWN_S))
        self._later(1, self._countdown_tick)
        self._watch()

    def _later(self, ms: float, callback) -> None:
        self.after_id = self.app.root.after(max(1, int(ms)), callback)

    def _watch(self) -> None:
        """Poll every few ms: Escape works even while another app is in front."""
        self.watch_id = None
        if not self.sending:
            return
        if wi.key_down(wi.VK_ESCAPE):
            self.stop("Escape was pressed")
            return
        if self.state == "typing" and time.perf_counter() >= self.settled_at:
            if self.target.external and wi.mouse_button_down():
                self.stop("a mouse button was clicked (that can move focus to another field)")
                return
            problem = self.target.focus_problem()
            if problem:
                self.stop(problem)
                return
        self.watch_id = self.app.root.after(WATCH_MS, self._watch)

    def _countdown_tick(self) -> None:
        self.after_id = None
        if self.state != "countdown":
            return
        problem = self.target.countdown_problem()
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
        self.started = now
        self.settled_at = now
        if self.target.external:
            self.target.activate()
            self.settled_at = now + ACTIVATE_SETTLE_S
        self.last_sent = self.settled_at
        self._schedule_next(self.last_sent)
        self.app.on_typing_started(self)
        self._tick()

    def _schedule_next(self, sent: float) -> None:
        self.planned_base = self.base_delay_ms()
        self.planned_ms = self.steps[self.index].delay_ms(self.planned_base)
        self.due = sent + self.planned_ms / 1000

    def _tick(self) -> None:
        """Send the next keystroke once it is due and it is safe to do so."""
        self.after_id = None
        if self.state != "typing":
            return
        now = time.perf_counter()
        if now < self.due - 0.0005:
            self._later((self.due - now) * 1000, self._tick)
            return
        problem = self.target.focus_problem()
        if problem:
            if self.index == 0 and self.target.external:
                problem = f"could not put keyboard focus back into {self.target.name()} ({problem})"
            self.stop(problem)
            return
        if wi.key_down(wi.VK_ESCAPE):
            self.stop("Escape was pressed")
            return
        if not self.target.external and not self._editor_in_sync(now, self._tick):
            return
        held = wi.held_modifiers()
        if held:
            # A held Ctrl/Alt/Shift/Windows key would turn the keystroke into a shortcut.
            self.held = True
            self.app.show_hold(held)
            self._later(25, self._tick)
            return
        self.target.before_keystroke()
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
            self.finished = sent
            self._finish()
            return
        self._schedule_next(sent)
        self._later(self.planned_ms, self._tick)

    def _editor_in_sync(self, now: float, retry) -> bool:
        """True if the Test editor shows exactly what was typed; otherwise wait briefly or stop."""
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
        elapsed = format_duration((self.finished - self.started) * 1000)
        fixes = f", {self.corrections} correction{'s' * (self.corrections != 1)}"
        count = f"{len(self.text)} characters{fixes}"
        if not self.target.external:
            if not self._editor_in_sync(time.perf_counter(), self._finish):
                return
            self.success = self.expected == self.text
            self._end("done", f"Done in {elapsed}: the test editor matches the input exactly ({count})."
                      if self.success else "Finished, but the test editor does not match the input.")
            return
        got = self.target.read_text()
        if got is not None and normalize_newlines(self.text) not in normalize_newlines(got):
            if self.sync_since is None:
                self.sync_since = time.perf_counter()
            if time.perf_counter() - self.sync_since < READBACK_TIMEOUT_S:
                self._later(50, self._finish)  # the other app may still be processing keys
                return
        where = self.target.name()
        if got is None:
            self.success = None
            message = (f"Done in {elapsed}: typed {count} into {where}. That app's text box "
                       f"can't be read back, so please check it by eye.")
        elif normalize_newlines(self.text) in normalize_newlines(got):
            self.success = True
            message = f"Done in {elapsed}: the text box in {where} contains the exact input ({count})."
        else:
            self.success = False
            message = f"Finished, but the text box in {where} does not contain the exact input."
        self._end("done", message)

    def stop(self, reason: str) -> None:
        if not self.active:
            return
        if self.state == "countdown":
            message = f"Stopped before typing started: {reason}."
        else:
            message = f"Stopped: {reason}. The text typed so far is left in {self.target.name()}."
        self._end("stopped", message)

    def _end(self, state: str, message: str) -> None:
        for attr in ("after_id", "watch_id"):
            pending = getattr(self, attr)
            if pending is not None:
                self.app.root.after_cancel(pending)
                setattr(self, attr, None)
        wi.end_timer_resolution(self._timer_period)
        self._timer_period = False
        self.state = state
        self.outcome = message
        self.clipboard_unchanged = wi.clipboard_sequence_number() == self.clipboard_seq
        self.app.on_run_ended(self)


# --- the window -----------------------------------------------------------------

class AutoTyperApp:
    def __init__(self, root: tk.Tk, seed: int | None = None, timing_log: str | None = None):
        self.root = root
        self.rng = random.Random(seed)
        self.timing_log = timing_log
        self.run: TypingRun | None = None
        self.target = EditorTarget(self)
        self.picking: dict | None = None
        self.run_ended_hook = None  # optional callable(run), used by the self-test
        self.wpm_var = tk.DoubleVar(value=WPM_DEFAULT)
        self.corrections_var = tk.BooleanVar(value=True)
        self.speed_text = tk.StringVar()
        self.target_text = tk.StringVar(value=self.target.describe())
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
        scale = max(1.0, root.winfo_fpixels("1i") / 96)  # follow Windows display scaling
        root.geometry(f"{int(940 * scale)}x{int(760 * scale)}")
        root.minsize(int(800 * scale), int(580 * scale))
        for name in ("TkDefaultFont", "TkTextFont"):
            tkfont.nametofont(name).configure(family="Segoe UI", size=10)
        style = ttk.Style(root)
        style.configure("Heading.TLabel", font=("Segoe UI Semibold", 11))
        style.configure("Hint.TLabel", foreground=MUTED)
        style.configure("Badge.TLabel", font=("Segoe UI Semibold", 11))
        style.configure("Target.TLabel", font=("Segoe UI Semibold", 10))
        style.configure("Detail.TLabel", foreground=MUTED, font=("Segoe UI", 9))

        outer = ttk.Frame(root, padding=(16, 12, 16, 12))
        outer.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        outer.rowconfigure(5, weight=1)

        ttk.Label(outer, text="Source text", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="Type here or paste with Ctrl+V. Start takes a snapshot.",
                  style="Hint.TLabel").grid(row=0, column=0, sticky="e")
        self.source = self._text_box(outer, row=1, undo=True)

        controls = ttk.Frame(outer)
        controls.grid(row=2, column=0, sticky="ew", pady=(12, 6))
        controls.columnconfigure(3, weight=1)
        ttk.Label(controls, text="Speed").grid(row=0, column=0, padx=(0, 8))
        self.speed_scale = ttk.Scale(controls, from_=WPM_MIN, to=WPM_MAX, length=200,
                                     variable=self.wpm_var, command=self._on_speed)
        self.speed_scale.grid(row=0, column=1)
        ttk.Label(controls, textvariable=self.speed_text, width=16).grid(row=0, column=2, padx=(8, 16))
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

        target_row = ttk.Frame(outer)
        target_row.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        target_row.columnconfigure(1, weight=1)
        ttk.Label(target_row, text="Type into").grid(row=0, column=0, padx=(0, 8))
        ttk.Label(target_row, textvariable=self.target_text, style="Target.TLabel").grid(
            row=0, column=1, sticky="w")
        self.pick_button = ttk.Button(target_row, text="Pick target…", width=14, command=self.toggle_pick)
        self.pick_button.grid(row=0, column=2, padx=(8, 6))
        self.editor_button = ttk.Button(target_row, text="Use test editor", width=14,
                                        command=self.use_test_editor)
        self.editor_button.grid(row=0, column=3)
        self.editor_button.state(["disabled"])

        ttk.Label(outer, text="Test editor", style="Heading.TLabel").grid(row=4, column=0, sticky="w")
        self.badge = ttk.Label(outer, textvariable=self.badge_text, style="Badge.TLabel")
        self.badge.grid(row=4, column=0, sticky="e")
        self.editor = self._text_box(outer, row=5, undo=False)

        status = ttk.Frame(outer)
        status.grid(row=6, column=0, sticky="ew", pady=(10, 0))
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
        text = tk.Text(frame, wrap="word", undo=undo, height=7, font=TEXT_FONT, padx=8, pady=6,
                       relief="flat", borderwidth=0, highlightthickness=2,
                       highlightbackground=BORDER, highlightcolor=ACCENT)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        return text

    def own_hwnd(self) -> int:
        return wi.toplevel_window(self.root.winfo_id())

    @property
    def wpm(self) -> int:
        return int(round(self.wpm_var.get()))

    def _on_speed(self, _value=None) -> None:
        self.wpm_var.set(self.wpm)  # snap to whole WPM
        self.speed_text.set(f"{self.wpm} WPM overall")

    def _set_badge(self, text: str, color: str = ACCENT) -> None:
        self.badge_text.set(text)
        self.badge.configure(foreground=color)

    def _set_idle_controls(self, idle: bool) -> None:
        on, off = ["!disabled"], ["disabled"]
        self.start_button.state(on if idle else off)
        self.corrections_check.state(on if idle else off)
        self.pick_button.state(on if idle else off)
        self.editor_button.state(on if idle and self.target.external else off)
        self.stop_button.state(off if idle else on)

    # Target picking ------------------------------------------------------------

    def toggle_pick(self) -> None:
        if self.picking:
            self._end_pick("Target picking cancelled.")
            return
        if self.run and self.run.active:
            return
        self.picking = {"hwnd": 0, "since": 0.0, "deadline": time.perf_counter() + PICK_TIMEOUT_S}
        self.pick_button.configure(text="Cancel picking")
        self.start_button.state(["disabled"])
        self.status_text.set("Now click inside the text box you want to type into, in another window "
                             "(a browser text field, Notepad, …). Esc cancels.")
        self.detail_text.set("")
        self._pick_poll()

    def _pick_poll(self) -> None:
        pick = self.picking
        if not pick:
            return
        now = time.perf_counter()
        if wi.key_down(wi.VK_ESCAPE):
            self._end_pick("Target picking cancelled.")
            return
        if now > pick["deadline"]:
            self._end_pick("No target was picked (timed out after 20 s).")
            return
        fg = wi.toplevel_window(wi.foreground_window())
        if fg and fg != self.own_hwnd() and not wi.is_shell_window(fg):
            if fg != pick["hwnd"]:
                pick["hwnd"], pick["since"] = fg, now
            elif now - pick["since"] >= PICK_SETTLE_S and not wi.mouse_button_down():
                focus = wi.focused_control()
                if focus and wi.toplevel_window(focus) != fg:
                    focus = 0
                self.target = WindowTarget(fg, focus)
                self._end_pick(f"Target set: {self.target.describe()}. Come back here and press Start; "
                               f"typing switches to it after the countdown.")
                wi.raise_without_activating(self.own_hwnd())
                return
        else:
            pick["hwnd"] = 0
        pick["after"] = self.root.after(100, self._pick_poll)

    def _end_pick(self, message: str) -> None:
        if self.picking and self.picking.get("after"):
            self.root.after_cancel(self.picking["after"])
        self.picking = None
        self.pick_button.configure(text="Pick target…")
        self.target_text.set(self.target.describe())
        self._set_idle_controls(True)
        self.status_text.set(message)

    def use_test_editor(self) -> None:
        if self.run and self.run.active:
            return
        self.target = EditorTarget(self)
        self.target_text.set(self.target.describe())
        self._set_idle_controls(True)
        self.status_text.set("Typing into the built-in test editor.")

    def _confirm_special_keys(self, text: str) -> bool:
        keys = [name for ch, name in (("\n", "line breaks (sent as Enter)"), ("\t", "tabs (sent as Tab)"))
                if ch in text]
        if not keys:
            return True
        return messagebox.askyesno(
            "Auto-Typer",
            f"The text contains {' and '.join(keys)}.\n\n"
            "In a single-line box Enter usually submits the form, and in most browser text "
            "boxes Tab jumps to the next field instead of typing a tab.\n\n"
            f"Type into {self.target.name()} anyway?",
            parent=self.root)

    # Focus ---------------------------------------------------------------------

    def _on_editor_focus_out(self, _event) -> None:
        if self.run and self.run.sending and not self.run.target.external:
            self.root.after_idle(self._recheck_focus)

    def _recheck_focus(self) -> None:
        if self.run and self.run.sending and not self.run.target.external:
            problem = self.run.target.focus_problem()
            if problem:
                self.run.stop(problem)

    # Run control ---------------------------------------------------------------

    def start(self) -> None:
        if (self.run and self.run.active) or self.picking:
            return
        text = self.source.get("1.0", "end-1c")  # the snapshot; later edits don't matter
        if not text:
            self.status_text.set("Nothing to type: enter or paste text in the source box first.")
            self.source.focus_set()
            return
        target = self.target
        if target.external:
            if not wi.is_window(target.hwnd):
                self.use_test_editor()
                self.status_text.set("The target window was closed, so the test editor is selected again.")
                return
            if not self._confirm_special_keys(text):
                return
        steps = plan_typing(text, corrections=self.corrections_var.get(), rng=self.rng)
        target.prepare()
        self.run = TypingRun(self, target, text, steps)
        self._set_idle_controls(False)
        self.progress.configure(maximum=len(steps), value=0)
        where = f"switches to {target.name()} and starts" if target.external else "starts"
        self.status_text.set(f"Get ready: typing {where} after the countdown. Esc or Stop cancels.")
        self.detail_text.set(f"{len(steps)} keystrokes planned, about {format_duration(self.run.remaining_ms())} "
                             f"at {self.wpm} WPM (ordinary keys about {self.run.base_delay_ms():.0f} ms apart).")
        self.run.start()

    def stop(self, reason: str) -> None:
        if self.run and self.run.active:
            self.run.stop(reason)

    def _on_escape(self, _event=None):
        if self.picking:
            self._end_pick("Target picking cancelled.")
            return "break"
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
        where = f" into {run.target.name()}" if run.target.external else ""
        self._set_badge(f"● Typing{where} (Esc to stop)")

    def on_step(self, run: TypingRun, rec: TimingRecord) -> None:
        step = rec.step
        self.progress.configure(value=run.index)
        percent = 100 * run.index // len(run.steps)
        self.status_text.set(f"Typing… {percent}% · about {format_duration(run.remaining_ms())} left · "
                             f"Esc or Stop cancels")
        detail = f"Last key {describe_key(step)}: {TIMING_NAMES[step.timing]} {step.key_factor:.2f}×"
        if step.pause:
            detail += f" + {PAUSE_NAMES[step.pause]} {step.pause_factor:.1f}× + {step.pause_ms:.0f} ms"
        if step.typo:
            detail += " (typo)"
        detail += (f" · waited {rec.actual_ms:.0f} ms (planned {rec.planned_ms:.0f} ms, "
                   f"base {rec.base_ms:.0f} ms)")
        self.detail_text.set(detail)

    def on_run_ended(self, run: TypingRun) -> None:
        self._set_idle_controls(True)
        clipboard = ("Clipboard untouched." if run.clipboard_unchanged else
                     "Note: the clipboard changed during the run (this app never writes to it).")
        self.status_text.set(f"{run.outcome} {clipboard}")
        if run.state == "done" and run.success:
            self._set_badge("✓ Exact match", SUCCESS)
        elif run.state == "done" and run.success is None:
            self._set_badge("✓ Done (check by eye)", SUCCESS)
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
