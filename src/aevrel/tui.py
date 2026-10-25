from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
import time
import textwrap
import warnings
from contextlib import redirect_stderr
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .dcp import migrate_dcp
from .demo import DemoError, run_demo
from .local.dcp import DCPStateProvider
from .local.drill import run_restore_drill
from .local.models import DrillOutcome
from .local.readiness import assess_readiness


@dataclass
class View:
    path: str
    record: dict[str, Any]
    readiness: dict[str, Any]


@dataclass
class TuiState:
    mode: str = "demo"
    transfer: str = "static"
    source: str = ""
    target_world: str = "3"
    output: str = ""
    work: str = ""
    drill_command: str = ""
    drill_steps: str = "10"
    drill_rank: str = "0"
    views: list[View] = field(default_factory=list)
    selected: int = 0
    scroll_offset: int = 0
    logs: list[str] = field(default_factory=list)
    task: str = ""
    progress: int = 0
    task_message: str = "idle"
    running: bool = False


def _view(
    path: str | Path,
    target_world: int | None = None,
    *,
    drill: DrillOutcome | None = None,
) -> View:
    resolved = Path(path).expanduser().resolve()
    record = DCPStateProvider().inventory(resolved)
    readiness = assess_readiness(
        record,
        target_world_size=target_world,
        drill=drill,
    )
    return View(
        path=str(resolved),
        record=record.to_dict(),
        readiness=readiness.to_dict(),
    )


class TuiApp:
    def __init__(self, work_dir: str | Path | None = None) -> None:
        self.root = (
            Path(work_dir).expanduser().resolve()
            if work_dir is not None
            else Path.home() / ".aevrel" / "tui"
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = TuiState()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.curses: Any = None
        self.screen: Any = None

    def run(self, screen: Any) -> None:
        self.curses = __import__("curses")
        self.screen = screen
        self._init_display(screen)
        self._log("TUI started")
        while True:
            self._drain_events()
            self._draw(screen)
            screen.timeout(120)
            try:
                key = screen.get_wch()
            except self.curses.error:
                key = None
            if self._handle_key(key):
                break

    def _init_display(self, screen: Any) -> None:
        curses = self.curses
        curses.curs_set(0)
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        for index, color in enumerate(
            (
                curses.COLOR_CYAN,
                curses.COLOR_GREEN,
                curses.COLOR_YELLOW,
                curses.COLOR_RED,
                curses.COLOR_MAGENTA,
            ),
            start=1,
        ):
            try:
                curses.init_pair(index, color, -1)
            except curses.error:
                pass
        screen.keypad(True)

    def _attr(self, pair: int = 0, *, bold: bool = False, reverse: bool = False) -> int:
        value = self.curses.color_pair(pair) if pair else 0
        if bold:
            value |= self.curses.A_BOLD
        if reverse:
            value |= self.curses.A_REVERSE
        return value

    def _log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.state.logs.append(f"{stamp} {message}")
        self.state.logs = self.state.logs[-8:]

    def _drain_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "progress":
                self.state.progress, self.state.task_message = payload
            elif kind == "done":
                task, result = payload
                self.state.running = False
                self.state.progress = 100
                self.state.task_message = "done"
                self._finish_task(task, result)
            elif kind == "error":
                self.state.running = False
                self.state.task_message = "failed"
                self._log(str(payload))

    def _finish_task(self, task: str, result: Any) -> None:
        if task == "demo":
            data = result
            views = data.get("views") or []
            for view in views:
                self._add_view(view)
            dcp = data.get("dcp") or {}
            if dcp.get("source"):
                self.state.source = str(dcp["source"])
                self.state.output = str(dcp.get("target") or "")
            self._log(
                "demo complete"
                if not data.get("dcp_error")
                else "demo complete: toy fallback; install aevrel[dcp]"
            )
            return
        if task == "inspect":
            self._add_view(result)
            self._log(f"checkpoint indexed: {Path(result.path).name}")
            return
        if task == "migrate":
            self._add_view(result["view"])
            migration = result["migration"]
            self._log(
                "migration complete: "
                f"{migration.get('verification', {}).get('details', 'verified')}"
            )
            return
        if task == "drill":
            self._add_view(result["view"])
            self._log(f"restore drill complete: {result['report']}")

    def _add_view(self, view: View) -> None:
        for index, current in enumerate(self.state.views):
            if current.path == view.path:
                self.state.views[index] = view
                self.state.selected = index
                return
        self.state.views.append(view)
        self.state.selected = len(self.state.views) - 1

    def _selected(self) -> View | None:
        if not self.state.views:
            return None
        self.state.selected = max(
            0,
            min(self.state.selected, len(self.state.views) - 1),
        )
        return self.state.views[self.state.selected]

    def _start(
        self,
        task: str,
        runner: Callable[[Callable[[int, str], None]], Any],
    ) -> None:
        if self.state.running:
            self._log("task already running")
            return
        self.state.running = True
        self.state.task = task
        self.state.progress = 0
        self.state.task_message = "starting"

        def progress(percent: int, message: str) -> None:
            self.events.put(
                ("progress", (max(0, min(100, percent)), message))
            )

        def work() -> None:
            try:
                result = runner(progress)
            except Exception as exc:
                self.events.put(("error", f"{task}: {exc}"))
            else:
                self.events.put(("done", (task, result)))

        threading.Thread(target=work, daemon=True).start()

    def _demo(self) -> None:
        def runner(progress: Callable[[int, str], None]) -> dict[str, Any]:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            root = self.root / f"demo-{stamp}"
            progress(5, "creating demo")
            try:
                result = run_demo(root, include_dcp=True)
            except DemoError as exc:
                if "PyTorch" not in str(exc):
                    raise
                progress(20, "PyTorch unavailable; toy mode")
                result = run_demo(
                    self.root / f"toy-{stamp}",
                    include_dcp=False,
                )
                result["dcp_error"] = str(exc)

            views: list[View] = []
            dcp = result.get("dcp")
            if isinstance(dcp, dict):
                drill = DrillOutcome(
                    phase="Succeeded",
                    succeeded=True,
                    proof_ref="tui-demo-dcp-load",
                )
                for key in ("source", "target"):
                    path = dcp.get(key)
                    if path:
                        views.append(
                            _view(path, target_world=3, drill=drill)
                        )
            result["views"] = views
            progress(95, "demo complete")
            return result

        self._start("demo", runner)

    def _inspect(self) -> None:
        path = self.state.source
        selected = self._selected()
        if not path and selected is not None:
            path = selected.path
        if not path:
            self._log("source checkpoint is empty")
            return

        def runner(progress: Callable[[int, str], None]) -> View:
            progress(20, "reading DCP metadata")
            return _view(path, self._target_world())

        self._start("inspect", runner)

    def _migrate(self) -> None:
        if self.state.transfer == "live":
            self._log("live rank split is not executable in local TUI")
            return
        source = self.state.source
        if not source:
            self._log("source checkpoint is empty")
            return
        target = self._target_world()
        if target is None:
            self._log("target world must be a positive integer")
            return
        source_path = Path(source).expanduser().resolve()
        output = (
            Path(self.state.output).expanduser().resolve()
            if self.state.output
            else source_path.with_name(f"{source_path.name}.tui-{target}")
        )
        work = (
            Path(self.state.work).expanduser().resolve()
            if self.state.work
            else source_path.with_name(
                f"{source_path.name}.tui-work-{target}"
            )
        )

        def runner(progress: Callable[[int, str], None]) -> dict[str, Any]:
            progress(3, "reading source")

            def event(payload: dict[str, Any]) -> None:
                planned = float(payload.get("bytes_planned") or 0)
                moved = float(payload.get("bytes_moved") or 0)
                phase = payload.get("phase")
                if phase == "verifying":
                    percent, message = 82, "verifying target"
                elif phase == "committing":
                    percent, message = 94, "committing target"
                elif planned:
                    percent = 8 + int(68 * moved / planned)
                    message = f"transfer {int(moved)}/{int(planned)} bytes"
                else:
                    percent, message = 20, "transferring"
                progress(percent, message)

            migration = migrate_dcp(
                source_path,
                output,
                work,
                target_world_size=target,
                progress_event=event,
            )
            progress(97, "indexing target")
            return {
                "migration": migration,
                "view": _view(output, target),
            }

        self._start("migrate", runner)

    def _drill(self) -> None:
        selected = self._selected()
        source = self.state.source or (selected.path if selected else "")
        if not source:
            self._log("source checkpoint is empty")
            return
        if not self.state.drill_command:
            self._log("restore drill command is empty")
            return
        try:
            steps = int(self.state.drill_steps)
            rank = int(self.state.drill_rank)
            target = self._target_world() or 1
        except ValueError:
            self._log("drill steps/rank must be integers")
            return
        report = (
            self.root
            / "drills"
            / time.strftime("%Y%m%d-%H%M%S")
            / f"rank{rank}.json"
        )

        def runner(progress: Callable[[int, str], None]) -> dict[str, Any]:
            progress(15, f"restore drill rank {rank}")
            result = run_restore_drill(
                source,
                target_message=self.state.drill_command,
                target_world_size=target,
                rank=rank,
                steps=steps,
                report_path=report,
            )
            progress(90, "indexing drill evidence")
            return {
                "drill": result,
                "view": _view(source, target),
                "report": str(report),
            }

        self._start("drill", runner)

    def _target_world(self) -> int | None:
        try:
            value = int(self.state.target_world)
        except ValueError:
            return None
        return value if value > 0 else None

    def _prompt(self, label: str, current: str) -> str | None:
        if self.screen is None:
            return None
        height, width = self.screen.getmaxyx()
        self.screen.timeout(-1)
        self.screen.move(height - 1, 0)
        self.screen.clrtoeol()
        prompt = f"{label}: "
        self.screen.addnstr(
            height - 1,
            0,
            prompt,
            max(1, width - 1),
            self._attr(1, bold=True),
        )
        try:
            self.curses.echo()
            self.curses.curs_set(1)
            raw = self.screen.getstr(
                height - 1,
                len(prompt),
                max(1, width - len(prompt) - 1),
            )
            return raw.decode("utf-8", "replace")
        except self.curses.error:
            return None
        finally:
            self.curses.noecho()
            self.curses.curs_set(0)
            self.screen.timeout(120)

    def _edit(self, key: str, label: str) -> None:
        current = getattr(self.state, key)
        value = self._prompt(label, current)
        if value is not None:
            setattr(self.state, key, value)
            self._log(f"{label} updated")

    def _handle_key(self, key: Any) -> bool:
        if key in ("q", "Q"):
            return True
        if key in ("d", "D"):
            self._demo()
        elif key in ("i", "I"):
            self._inspect()
        elif key in ("m", "M"):
            self._migrate()
        elif key in ("r", "R"):
            self._drill()
        elif key in ("e", "E"):
            self._edit("source", "source")
        elif key in ("o", "O"):
            self._edit("output", "output")
        elif key in ("w", "W"):
            self._edit("work", "work dir")
        elif key in ("t", "T"):
            self._edit("target_world", "target world")
        elif key in ("c", "C"):
            self._edit("drill_command", "drill command")
        elif key in ("n", "N"):
            self._edit("drill_steps", "drill steps")
        elif key in ("k", "K"):
            self._edit("drill_rank", "drill rank")
        elif key in ("v", "V"):
            self.state.mode = (
                "real" if self.state.mode == "demo" else "demo"
            )
            self._log(f"mode: {self.state.mode}")
        elif key in ("1", "2"):
            self.state.mode = "demo" if key == "1" else "real"
            self._log(f"mode: {self.state.mode}")
        elif key in ("b", "B", "l", "L"):
            self.state.transfer = (
                "live" if self.state.transfer == "static" else "static"
            )
            self._log(f"transfer: {self.state.transfer}")
        elif key in (self.curses.KEY_UP,):
            if self.state.views:
                self.state.selected = max(0, self.state.selected - 1)
                if self.state.selected < self.state.scroll_offset:
                    self.state.scroll_offset = self.state.selected
        elif key in (self.curses.KEY_DOWN,):
            if self.state.views:
                limit = getattr(self, "_list_limit", 7)
                self.state.selected = min(
                    len(self.state.views) - 1,
                    self.state.selected + 1,
                )
                if self.state.selected >= self.state.scroll_offset + limit:
                    self.state.scroll_offset = self.state.selected - limit + 1
                if self.state.scroll_offset < 0:
                    self.state.scroll_offset = 0
        return False

    def _draw(self, screen: Any) -> None:
        try:
            screen.erase()
            height, width = screen.getmaxyx()
            if height < 24 or width < 80:
                self._draw_too_small(screen, height, width)
                return
            if height < 30 or width < 100:
                self._draw_compact(screen, height, width)
                return
            self._draw_header(screen, width)
            footer = 4
            body_y = 2
            body_h = height - body_y - footer
            left_w = 38
            right_w = 31
            center_x = left_w + 1
            center_w = width - left_w - right_w - 2
            right_x = width - right_w
            self._draw_left(screen, body_y, 0, body_h, left_w)
            self._draw_center(screen, body_y, center_x, body_h, center_w)
            self._draw_right(screen, body_y, right_x, body_h, right_w)
            self._draw_footer(screen, height - footer, width)
            screen.refresh()
        except self.curses.error:
            pass

    def _draw_too_small(self, screen, height, width) -> None:
        sig = (height, width)
        if getattr(self, "_last_small", None) == sig:
            return
        self._last_small = sig
        try:
            screen.erase()
            self._safe(
                screen,
                0,
                0,
                f"terminal too small: {width}x{height}; need >= 80x24",
                self._attr(3, bold=True),
            )
            screen.refresh()
        except Exception:
            pass

    def _draw_compact(self, screen, height, width) -> None:
        self._draw_header(screen, width)
        row = 2
        s = self.state
        for label, value in (
            ("MODE", s.mode),
            ("TRANSFER", s.transfer),
            ("SOURCE", s.source or "(empty)"),
            ("TARGET", s.target_world),
            ("OUTPUT", s.output or "(auto)"),
            ("WORK", s.work or "(auto)"),
        ):
            if row >= height - 2:
                break
            self._safe(screen, row, 0, f"{label:<10} {value}", self._attr(0))
            row += 1
        row += 1
        if row < height - 2:
            self._safe(screen, row, 0, "CHECKPOINTS", self._attr(1, bold=True))
            row += 1
        limit = min(7, height - row - 4)
        offset = max(
            0,
            min(s.scroll_offset, max(0, len(s.views) - limit)),
        )
        for win_index, view in enumerate(s.views[offset : offset + limit]):
            if row >= height - 2:
                break
            abs_index = offset + win_index
            marker = ">" if abs_index == s.selected else " "
            status = view.readiness.get("status", "UNKNOWN")
            self._safe(
                screen,
                row,
                0,
                f"{marker} {Path(view.path).name[:max(12, width - 22)]}  [{status}]",
                self._status_attr(status),
            )
            row += 1
        self._draw_footer(screen, height - 4, width)

    def _draw_header(self, screen: Any, width: int) -> None:
        title = (
            f"AEVREL {__version__}  CHECKPOINT CONTROL PLANE  "
            f"LOCAL  {time.strftime('%H:%M:%S')}"
        )
        self._safe(screen, 0, 0, title, self._attr(1, bold=True))
        self._safe(
            screen,
            1,
            0,
            "=" * max(1, width - 1),
            self._attr(0, bold=True),
        )

    def _draw_footer(self, screen: Any, y: int, width: int) -> None:
        if self.state.running:
            bar_width = max(10, width - 52)
            filled = int(bar_width * self.state.progress / 100)
            bar = "#" * filled + "." * (bar_width - filled)
            line = (
                f"{self.state.task.upper():8} [{bar}] "
                f"{self.state.progress:3d}%  {self.state.task_message}"
            )
            attr = self._attr(2, bold=True)
        else:
            line = "READY"
            attr = self._attr(2)
        self._safe(screen, y, 0, line, attr)
        logs = self.state.logs[-2:]
        for index, item in enumerate(logs):
            self._safe(
                screen,
                y + 1 + index,
                0,
                item,
                self._attr(0),
            )
        keys = (
            "D Demo  I Inspect  M Migrate  R Drill  E Source  O Output  "
            "W Work  T Target  C Cmd  N Steps  K Rank  V Mode  B Transfer  Q Quit"
        )
        self._safe(screen, y + 3, 0, keys, self._attr(0, bold=True))

    def _draw_left(
        self,
        screen: Any,
        y: int,
        x: int,
        h: int,
        w: int,
    ) -> None:
        self._box(screen, y, x, h, w, "TRANSFER")
        row = y + 2
        self._safe(screen, row, x + 2, "[MODE]   DEMO" if self.state.mode == "demo" else "[MODE]   REAL", self._attr(1))
        row += 1
        transfer = "STATIC" if self.state.transfer == "static" else "LIVE (EXPERIMENTAL)"
        attr = self._attr(4 if self.state.transfer == "live" else 1)
        self._safe(screen, row, x + 2, f"[TYPE]   {transfer}", attr)
        row += 2
        row = self._field(screen, row, x + 2, w - 4, "SOURCE", self.state.source or "(empty)")
        row = self._field(screen, row, x + 2, w - 4, "TARGET", self.state.target_world)
        row = self._field(screen, row, x + 2, w - 4, "OUTPUT", self.state.output or "(auto)")
        row = self._field(screen, row, x + 2, w - 4, "WORK", self.state.work or "(auto)")
        row += 1
        self._safe(screen, row, x + 2, "D  generate demo", self._attr(1))
        row += 1
        self._safe(screen, row, x + 2, "I  inspect checkpoint", self._attr(1))
        row += 1
        self._safe(screen, row, x + 2, "M  migrate static", self._attr(1))
        row += 1
        self._safe(screen, row, x + 2, "R  restore drill", self._attr(1))
        row += 2
        if self.state.transfer == "live":
            for line in textwrap.wrap(
                "Live is unavailable in local TUI; "
                "controller + live worker + GPU are required.",
                max(10, w - 4),
            ):
                self._safe(screen, row, x + 2, line, self._attr(4))
                row += 1
        else:
            self._safe(
                screen,
                row,
                x + 2,
                "E/O/W/T edit fields",
                self._attr(0, bold=True),
            )

    def _draw_center(
        self,
        screen: Any,
        y: int,
        x: int,
        h: int,
        w: int,
    ) -> None:
        self._box(screen, y, x, h, w, "CONTROL PLANE")
        row = y + 2
        self._safe(screen, row, x + 2, "CHECKPOINTS", self._attr(1, bold=True))
        row += 1
        if not self.state.views:
            self._safe(
                screen,
                row,
                x + 2,
                "no checkpoint loaded; press D or I",
                self._attr(0),
            )
            row += 2
        else:
            limit = min(7, h - 12)
            self._list_limit = limit
            offset = max(
                0,
                min(
                    self.state.scroll_offset,
                    max(0, len(self.state.views) - limit),
                ),
            )
            self.state.scroll_offset = offset
            for win_index, view in enumerate(
                self.state.views[offset : offset + limit]
            ):
                abs_index = offset + win_index
                marker = ">" if abs_index == self.state.selected else " "
                status = view.readiness.get("status", "UNKNOWN")
                line = (
                    f"{marker} {Path(view.path).name[:max(12, w - 22)]}"
                    f"  [{status}]"
                )
                attr = self._status_attr(status)
                if abs_index == self.state.selected:
                    attr |= self.curses.A_REVERSE
                self._safe(screen, row, x + 2, line, attr)
                row += 1
            row += 1
            row = self._draw_readiness_legend(screen, row, x + 2, y, h)
        self._safe(screen, row, x + 2, "DETAIL", self._attr(1, bold=True))
        row += 1
        view = self._selected()
        if view is None:
            self._safe(screen, row, x + 2, "nothing selected", self._attr(0))
            return
        readiness = view.readiness
        record = view.record
        details = [
            ("PATH", view.path),
            ("STATUS", readiness.get("status", "UNKNOWN")),
            ("COMPLETE", self._bool(record.get("complete"))),
            ("FRESH", self._bool(readiness.get("fresh"))),
            ("LOADABLE", self._bool(readiness.get("loadable"))),
            ("MOVABLE", self._bool(readiness.get("movable"))),
        ]
        for label, value in details:
            if row >= y + h - 2:
                return
            self._safe(
                screen,
                row,
                x + 2,
                f"{label:<10} {value}",
                self._status_attr(str(value)) if label == "STATUS" else self._attr(0),
            )
            row += 1
        reasons = readiness.get("reasons") or []
        if reasons and row < y + h - 2:
            self._safe(screen, row, x + 2, "REASONS", self._attr(3, bold=True))
            row += 1
            for reason in reasons[:2]:
                text = f"{reason.get('code')}: {reason.get('detail')}"
                for line in textwrap.wrap(text, max(20, w - 4))[:2]:
                    if row >= y + h - 2:
                        return
                    self._safe(screen, row, x + 2, line, self._attr(3))
                    row += 1
        ranks = record.get("ranks") or []
        if ranks and row < y + h - 1:
            cells = " ".join(
                f"R{item.get('rank')}:{len(item.get('shards') or [])}"
                for item in ranks[:12]
            )
            self._safe(screen, row, x + 2, f"RANKS     {cells}", self._attr(1))

    def _draw_right(
        self,
        screen: Any,
        y: int,
        x: int,
        h: int,
        w: int,
    ) -> None:
        self._box(screen, y, x, h, w, "CONTEXT")
        row = y + 2
        kubeconfig = os.environ.get("KUBECONFIG") or str(
            Path.home() / ".kube" / "config"
        )
        kube = bool(shutil.which("kubectl") and Path(kubeconfig).exists())
        # provenance: verified = real check (kubectl + kubeconfig present);
        # mode-flag = real scheduler/runtime mode flag; na = not verified locally
        groups = [
            (
                "UPSTREAM",
                [
                    ("Kubernetes", "OK" if kube else "--", "verified"),
                    ("Training Job", "--", "na"),
                    ("Checkpoint store", "--", "na"),
                    ("Source agent", "OPT", "mode-flag"),
                ],
            ),
            (
                "DOWNSTREAM",
                [
                    ("Kueue", "OK" if kube else "--", "verified"),
                    ("Restore drill", "--", "na"),
                    ("Migration", "--", "na"),
                    ("Target load", "--", "na"),
                ],
            ),
            (
                "INTEGRATIONS",
                [
                    ("Live rank split", "EXP", "mode-flag"),
                    ("Proof / audit", "--", "na"),
                    ("Rescue decision", "K8S", "mode-flag"),
                ],
            ),
        ]
        for title, items in groups:
            if row >= y + h - 2:
                return
            self._safe(screen, row, x + 2, title, self._attr(1, bold=True))
            row += 1
            for label, status, provenance in items:
                if row >= y + h - 2:
                    return
                if provenance == "verified" and status == "OK":
                    color = 2
                elif provenance == "mode-flag":
                    color = 1
                else:
                    color = 0
                self._safe(
                    screen,
                    row,
                    x + 2,
                    f"[{status:<3}] {label}",
                    self._attr(color),
                )
                row += 1
            row += 1

    def _field(
        self,
        screen: Any,
        row: int,
        x: int,
        width: int,
        label: str,
        value: str,
    ) -> int:
        self._safe(screen, row, x, label, self._attr(0, bold=True))
        row += 1
        for line in textwrap.wrap(str(value), max(8, width))[:2]:
            self._safe(screen, row, x, line, self._attr(0))
            row += 1
        return row + 1

    def _box(
        self,
        screen: Any,
        y: int,
        x: int,
        h: int,
        w: int,
        title: str,
    ) -> None:
        if h < 3 or w < 4:
            return
        border = self._attr(0)
        self._safe(screen, y, x, "+" + "-" * (w - 2) + "+", border)
        self._safe(screen, y + h - 1, x, "+" + "-" * (w - 2) + "+", border)
        for row in range(y + 1, y + h - 1):
            self._safe(screen, row, x, "|", border)
            self._safe(screen, row, x + w - 1, "|", border)
        self._safe(
            screen,
            y,
            x + 2,
            f" {title} ",
            self._attr(1, bold=True),
        )

    def _status_attr(self, status: str) -> int:
        if status == "READY":
            return self._attr(2, bold=True)
        if status in {"UNVERIFIED", "STALE"}:
            return self._attr(3)
        if status in {"INCOMPLETE", "TARGET_BLOCKED"}:
            return self._attr(4)
        return self._attr(0)

    def _draw_readiness_legend(self, screen, row, x, y, h) -> int:
        legend = [
            ("READY", self._status_attr("READY")),
            ("UNVERIFIED/STALE", self._status_attr("STALE")),
            ("INCOMPLETE/BLOCKED", self._status_attr("INCOMPLETE")),
            ("UNKNOWN", self._status_attr("UNKNOWN")),
        ]
        if row < y + h - 2:
            self._safe(screen, row, x, "LEGEND:", self._attr(1, bold=True))
            row += 1
        for text, attr in legend:
            if row >= y + h - 2:
                break
            self._safe(screen, row, x, f"  {text}", attr)
            row += 1
        return row + 1

    @staticmethod
    def _bool(value: Any) -> str:
        if value is True:
            return "true"
        if value is False:
            return "false"
        return "unknown"

    def _safe(
        self,
        screen: Any,
        y: int,
        x: int,
        text: str,
        attr: int = 0,
    ) -> None:
        height, width = screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        try:
            screen.addnstr(
                y,
                x,
                str(text),
                max(1, width - x - 1),
                attr,
            )
        except self.curses.error:
            pass


def run_tui(work_dir: str | Path | None = None) -> int:
    if not sys.stdout.isatty():
        print("aevrel tui requires an interactive terminal", file=sys.stderr)
        return 2
    try:
        import curses
    except ImportError:
        print(
            "aevrel tui requires a POSIX curses terminal "
            "(Linux, macOS, or WSL)",
            file=sys.stderr,
        )
        return 2
    warnings.filterwarnings("ignore")
    log_path = (
        Path(work_dir).expanduser() / "tui.log"
        if work_dir is not None
        else Path.home() / ".aevrel" / "tui.log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("a", encoding="utf-8") as log:
            with redirect_stderr(log):
                curses.wrapper(TuiApp(work_dir).run)
    except KeyboardInterrupt:
        pass
    return 0
