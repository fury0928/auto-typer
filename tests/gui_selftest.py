"""End-to-end checks on a real Windows desktop, using real SendInput keystrokes.

    py tests\\gui_selftest.py [--timing-log full_run.csv]

Opens the auto-typer, drives it through the scenarios below and prints PASS or
FAIL for each check. It takes about two minutes. Don't touch the keyboard or
mouse while it runs: the app will (correctly) treat that as a focus change.
Exit code: 0 = all passed, 1 = a check failed, 2 = could not get the foreground.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import statistics
import subprocess
import sys
import time
import tkinter as tk
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autotyper  # noqa: E402
import win_input as wi  # noqa: E402
from typing_plan import BACKSPACE, BURST_RANGE, LETTER_RANGE, PAUSES, SLOW_RANGE  # noqa: E402

FAMILY = "👩‍👩‍👧"
SAMPLE = (
    "Hello world. This is a typing test: mail test@example.com about #42, 50% off, 3+4=7!\n"
    "\n"
    "Café, naïve, jalapeño, and é with a combining accent.\tAfter a tab.  Two spaces.\n"
    "Emoji: 😊 👍🏽 " + FAMILY + " 🇺🇸 1️⃣ and “curly quotes” — dash.\n"
    "\n"
    "\n"
    "Last paragraph after two blank lines. The quick brown fox jumps over the lazy dog. \n"
)
MEDIUM = "Interference test: the caret is moved and the slider is clicked mid-run, ok? 😊 done.\n"
LONG = "the quick brown fox jumps over the lazy dog " * 20

THIEF_TITLE = "Auto-typer self-test: focus thief"
THIEF = f"""
import ctypes, tkinter as tk
r = tk.Tk()
r.title({THIEF_TITLE!r})
r.geometry("360x90+140+140")
tk.Label(r, text="Simulated other application").pack(expand=True)
r.attributes("-topmost", True)
r.update()
ctypes.windll.user32.SetForegroundWindow(ctypes.windll.user32.GetAncestor(r.winfo_id(), 2))
r.focus_force()
r.after(2500, r.destroy)
r.mainloop()
"""

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
_user32.FindWindowW.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
_user32.GetClipboardOwner.restype = wintypes.HWND
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD))


def describe_window(hwnd: int) -> str:
    """Title and executable of a window, to explain an unexpected focus change."""
    if not hwnd:
        return "no foreground window"
    title = ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(hwnd, title, 256)
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = "?"
    handle = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        buf, size = ctypes.create_unicode_buffer(512), wintypes.DWORD(512)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            exe = os.path.basename(buf.value)
        _kernel32.CloseHandle(handle)
    return f"{title.value!r} ({exe}, pid {pid.value})"


class Harness:
    def __init__(self, timing_log: str | None):
        wi.enable_dpi_awareness()
        self.root = tk.Tk()
        self.app = autotyper.AutoTyperApp(self.root, seed=20261117)
        self.app.run_ended_hook = self._run_ended
        self.timing_log = timing_log
        self.results: list[tuple[str, bool, str]] = []
        self.clipboard_start = wi.clipboard_sequence_number()
        self.thief: subprocess.Popen | None = None
        self.scenario: dict | None = None
        self.beats: list[float] = []
        self.beat_info: list[str] = []
        self.exit_code = 0
        self.scenarios = [
            dict(name="Full run, corrections on, 150 WPM", text=SAMPLE, wpm=150, corrections=True,
                 heartbeat=True, log=True, check=self.check_full_run),
            dict(name="Corrections off, 200 WPM", text=SAMPLE.split("\n\n\n")[0], wpm=200,
                 corrections=False, check=self.check_corrections_off),
            dict(name="Caret moved and slider clicked mid-run", text=MEDIUM, wpm=120, corrections=True,
                 actions=[(15, self.move_caret), (30, self.click_slider)], check=self.check_interference),
            dict(name="Stop button", text=LONG, wpm=200, corrections=True,
                 actions=[(25, lambda: self.app.stop_button.invoke())],
                 check=lambda run, text: self.check_stopped(run, text, "Stop button")),
            dict(name="Escape key (real keystroke via SendInput)", text=LONG, wpm=200, corrections=True,
                 actions=[(25, self.press_escape)],
                 check=lambda run, text: self.check_stopped(run, text, "Escape")),
            dict(name="Focus moved to the source box", text=LONG, wpm=200, corrections=True,
                 actions=[(25, lambda: self.app.source.focus_set())],
                 check=lambda run, text: self.check_stopped(run, text, "focus left the test editor")),
            dict(name="Focus lost during the countdown", text=LONG, wpm=200, corrections=True,
                 actions=[("countdown", lambda: self.app.source.focus_set())],
                 check=self.check_countdown_stop),
            dict(name="Another application takes the foreground", text=LONG, wpm=200, corrections=True,
                 actions=[(25, self.spawn_thief)], timeout=15000,
                 check=lambda run, text: self.check_stopped(run, text, "another window became active")),
        ]

    # Plumbing ------------------------------------------------------------------

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)

    def hwnd(self) -> int:
        return wi.toplevel_window(self.root.winfo_id())

    def main(self) -> int:
        print(f"Windows {platform.release()} ({platform.version()}), Python {platform.python_version()}, "
              f"Tk {self.root.tk.call('info', 'patchlevel')}", flush=True)
        self.root.geometry("+60+60")
        self.root.attributes("-topmost", True)
        self.root.after(300, self.acquire_foreground, time.perf_counter(), self.next_scenario)
        self.root.mainloop()
        if self.thief and self.thief.poll() is None:
            self.thief.terminate()
        return self.exit_code

    def acquire_foreground(self, t0: float, then) -> None:
        if wi.foreground_window() == self.hwnd():
            then()
            return
        if time.perf_counter() - t0 > 5:
            print("SKIPPED: the test window could not get the foreground (locked or minimised session?)")
            self.exit_code = 2
            self.root.destroy()
            return
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.root.after(200, self.acquire_foreground, t0, then)

    def next_scenario(self) -> None:
        if not self.scenarios:
            self.finish()
            return
        sc = self.scenario = self.scenarios.pop(0)
        print(f"\n== {sc['name']}", flush=True)
        app = self.app
        app.timing_log = self.timing_log if sc.get("log") else None
        app.source.delete("1.0", "end")
        app.source.insert("1.0", sc["text"])
        app.wpm_var.set(sc["wpm"])
        app._on_speed()
        app.corrections_var.set(sc["corrections"])
        sc["pending"] = list(sc.get("actions", []))
        # Like a real paste: let the source box draw the text (Tk's first lookup of
        # fallback fonts for new emoji can take a few hundred ms) before Start.
        self.root.after(800, self.press_start, sc)

    def press_start(self, sc: dict) -> None:
        if wi.foreground_window() != self.hwnd():
            self.root.after(10, self.acquire_foreground, time.perf_counter(), lambda: self.press_start(sc))
            return
        sc["t0"] = time.perf_counter()
        self.app.start_button.invoke()
        if sc.get("heartbeat"):
            self.beats, self.beat_info = [time.perf_counter()], ["start"]
            self.root.after(20, self.heartbeat)
        self.root.after(15, self.watch)
        sc["guard"] = self.root.after(sc.get("timeout", 180_000), self.timeout)

    def watch(self) -> None:
        """Fire scenario actions when the run reaches the right step."""
        run, sc = self.app.run, self.scenario
        if not run or not run.active or not sc["pending"]:
            return
        trigger, action = sc["pending"][0]
        due = (trigger == "countdown" and run.state == "countdown"
               and time.perf_counter() - sc["t0"] > 0.6) or (
               isinstance(trigger, int) and run.state == "typing" and run.index >= trigger)
        if due:
            sc["pending"].pop(0)
            action()
        self.root.after(5, self.watch)

    def heartbeat(self) -> None:
        run = self.app.run
        self.beats.append(time.perf_counter())
        self.beat_info.append(f"{run.state} step {run.index}" if run else "?")
        if run and run.active:
            self.root.after(20, self.heartbeat)

    def timeout(self) -> None:
        self.app.stop("self-test timeout")

    def _run_ended(self, run) -> None:
        if "another window" in run.outcome:
            print(f"  foreground is now {describe_window(wi.foreground_window())}", flush=True)
        if not run.clipboard_unchanged:  # read-only: who owns the clipboard now?
            print(f"  clipboard was changed by {describe_window(_user32.GetClipboardOwner() or 0)}", flush=True)
        self.root.after_cancel(self.scenario["guard"])
        text_at_end = self.app.editor.get("1.0", "end-1c")
        self.root.after(700, self.evaluate, run, text_at_end)

    def evaluate(self, run, text_at_end: str) -> None:
        print(f"  status: {self.app.status_text.get()}", flush=True)
        if self.app.detail_text.get():
            print(f"  report: {self.app.detail_text.get()}", flush=True)
        self.check("no keystrokes arrive after the run ended",
                   self.app.editor.get("1.0", "end-1c") == text_at_end)
        self.check("clipboard untouched during the run", run.clipboard_unchanged)
        self.scenario["check"](run, text_at_end)
        if self.scenarios:
            self.root.after(300, self.acquire_foreground, time.perf_counter(), self.next_scenario)
        else:
            self.finish()

    def finish(self) -> None:
        self.check("clipboard sequence number unchanged across the whole self-test",
                   wi.clipboard_sequence_number() == self.clipboard_start)
        failed = [name for name, ok, _ in self.results if not ok]
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} checks passed.", flush=True)
        for name in failed:
            print(f"  FAILED: {name}")
        if failed and self.exit_code == 0:
            self.exit_code = 1
        self.root.after(500, self.root.destroy)

    # Scenario actions ------------------------------------------------------------

    def move_caret(self) -> None:
        """What a stray click inside the editor does: caret to the start plus a selection."""
        editor = self.app.editor
        editor.mark_set("insert", "1.0")
        editor.tag_add("sel", "1.0", "1.4")

    def click_slider(self) -> None:
        scale = self.app.speed_scale
        x, y = scale.winfo_width() - 4, scale.winfo_height() // 2
        scale.event_generate("<ButtonPress-1>", x=x, y=y)
        scale.event_generate("<ButtonRelease-1>", x=x, y=y)
        self.app.wpm_var.set(180)  # then "drag" it further, as a user would
        self.app._on_speed()

    def press_escape(self) -> None:
        if wi.foreground_window() == self.hwnd():  # never inject into another window
            wi.press_escape()

    def spawn_thief(self) -> None:
        """Open another application's window and switch to it, like the user clicking it.

        Windows blocks a newly started app from grabbing the foreground while
        keystrokes are flowing, so the switch is made from this (foreground) process.
        """
        self.thief = subprocess.Popen([sys.executable, "-c", THIEF])
        self.root.after(50, self.activate_thief, time.perf_counter())

    def activate_thief(self, t0: float) -> None:
        hwnd = _user32.FindWindowW(None, THIEF_TITLE)
        if hwnd:
            _user32.SetForegroundWindow(hwnd)
        elif time.perf_counter() - t0 < 10:
            self.root.after(20, self.activate_thief, t0)

    # Checks ----------------------------------------------------------------------

    def ratios(self, run, select) -> list[float]:
        return [r.actual_ms / r.base_ms for r in run.records if not r.held and select(r.step)]

    def check_full_run(self, run, text: str) -> None:
        base = run.records[-1].base_ms
        tol = 6.0 / base  # timer slack, expressed as a ratio
        self.check("final text matches the input exactly (incl. spaces, blank lines, Unicode)",
                   run.state == "done" and run.success and text == SAMPLE,
                   f"{len(text)} vs {len(SAMPLE)} code points")

        def in_range(kind: str, lo: float, hi: float) -> tuple[bool, str]:
            """Planned factors must all be in range; measured waits 97% (OS scheduling noise)."""
            planned = [s.key_factor for s in run.steps if s.timing == kind]
            measured = self.ratios(run, lambda s: s.timing == kind and not s.pause)
            inside = sum(lo - tol <= r <= hi + tol for r in measured)
            ok = (planned and all(lo <= f <= hi for f in planned)
                  and measured and inside >= 0.97 * len(measured))
            return ok, (f"measured n={len(measured)}, {min(measured or [0]):.2f}–{max(measured or [0]):.2f}×, "
                        f"mean {statistics.fmean(measured or [0]):.2f}×, {len(measured) - inside} outside")

        ok, detail = in_range("slow", *SLOW_RANGE)
        self.check("symbols and non-ASCII wait 1.5–2.5× the base delay", ok, detail)
        ok, detail = in_range("letter", *LETTER_RANGE)
        letters = self.ratios(run, lambda s: s.timing == "letter" and not s.pause)
        self.check(f"ordinary keys vary within {LETTER_RANGE[0]}–{LETTER_RANGE[1]}× of the base delay",
                   ok and statistics.pstdev(letters) > 0.1, f"{detail}, sd {statistics.pstdev(letters):.2f}")
        ok, detail = in_range("burst", *BURST_RANGE)
        bursts = self.ratios(run, lambda s: s.timing == "burst" and not s.pause)
        self.check(f"fast bursts run at {BURST_RANGE[0]}–{BURST_RANGE[1]}× the base delay",
                   ok and statistics.fmean(bursts) < statistics.fmean(letters), detail)
        sentence = self.ratios(run, lambda s: s.pause == "sentence")
        paragraph = self.ratios(run, lambda s: s.pause == "paragraph")
        self.check("pauses after sentence ends and paragraph breaks are longer",
                   sentence and paragraph and min(sentence) > max(letters)
                   and statistics.fmean(paragraph) > statistics.fmean(sentence),
                   f"sentence {statistics.fmean(sentence):.1f}× (n={len(sentence)}), "
                   f"paragraph {statistics.fmean(paragraph):.1f}× (n={len(paragraph)})")
        units = {r.step.text for r in run.records}
        combined = ("é", "👍🏽", FAMILY, "🇺🇸", "1️⃣")
        self.check("combined accents and multi-part emoji are sent as one timed unit",
                   all(u in units for u in combined))

        notice_min = PAUSES["notice"][0] * base + PAUSES["notice"][2]
        notices = [r for r in run.records if r.step.pause == "notice"]
        wrong = [snap for snap in run.correction_snapshots if not SAMPLE.startswith(snap)]
        self.check("typos are shown, paused on, erased with Backspace and retyped",
                   run.corrections >= 2 and len(wrong) == len(run.correction_snapshots) == run.corrections
                   and all(r.actual_ms >= notice_min - 6 for r in notices)
                   and any(r.step.action == BACKSPACE for r in run.records),
                   f"{run.corrections} corrections, e.g. "
                   + ", ".join(repr(s[-12:]) for s in run.correction_snapshots[:3]))

        errors = sorted(abs(r.actual_ms - r.planned_ms) for r in run.records if not r.held)
        p95 = errors[int(0.95 * (len(errors) - 1))]
        self.check("keystrokes are sent on schedule (95th percentile error < 8 ms)", p95 < 8,
                   f"median {statistics.median(errors):.1f} ms, p95 {p95:.1f} ms, max {errors[-1]:.1f} ms")
        gaps = [b - a for a, b in zip(self.beats, self.beats[1:])]
        worst = max(range(len(gaps)), key=gaps.__getitem__)
        slow = sorted((g * 1000 for g in gaps if g > 0.06), reverse=True)
        self.check("UI stays responsive (20 ms heartbeat never delayed past 100 ms)",
                   max(gaps) < 0.1,
                   f"max gap {gaps[worst] * 1000:.0f} ms between '{self.beat_info[worst]}' and "
                   f"'{self.beat_info[worst + 1]}'; gaps > 60 ms: {[round(g) for g in slow[:5]]}")

    def check_corrections_off(self, run, text: str) -> None:
        self.check("exact text with corrections off", run.success and text == run.target)
        self.check("no Backspace or typo keystrokes when corrections are off",
                   not any(r.step.action == BACKSPACE or r.step.typo for r in run.records))

    def check_interference(self, run, text: str) -> None:
        bases = sorted({round(r.base_ms, 1) for r in run.records})
        self.check("moving the caret / selecting text mid-run does not corrupt the output",
                   run.success and text == MEDIUM)
        self.check("slider changes speed live without stealing focus", len(bases) >= 2 and run.success,
                   f"base delays used: {bases} ms")

    def check_stopped(self, run, text: str, reason: str) -> None:
        self.check(f"run stopped because {reason}", run.state == "stopped" and reason in run.outcome,
                   run.outcome)
        self.check("text typed so far is left visible in the editor",
                   len(text) > 0 and text == run.expected, f"{len(text)} characters kept")

    def check_countdown_stop(self, run, text: str) -> None:
        self.check("focus change during the countdown stops before any keystroke",
                   run.state == "stopped" and "before typing started" in run.outcome
                   and not run.records and text == "", run.outcome)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--timing-log", help="CSV path for the full run's per-keystroke timings")
    sys.exit(Harness(parser.parse_args().timing_log).main())
