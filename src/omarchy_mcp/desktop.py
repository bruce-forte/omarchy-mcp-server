"""The parts of the desktop that Omarchy's command registry does not describe.

Window layout comes from Hyprland, pixels come from grim, text comes from
tesseract, and the clipboard comes from wl-clipboard. None of these are
`omarchy` commands, so none of them are reachable through `omarchy_run` -- which
is exactly why the tools built on this module earn their place.

Everything here is in `omarchy-base.packages`, so it is present on every Omarchy
install.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

HYPRCTL_TIMEOUT_S = 5
GRIM_TIMEOUT_S = 15
OCR_TIMEOUT_S = 30

#: Claude resizes anything larger than this before looking at it, so sending
#: more is paying transport for pixels nothing will read.
DEFAULT_MAX_WIDTH = 1568

#: The flags `omarchy capture text` uses. Matching them means OCR here reads the
#: same as OCR through the desktop shortcut.
TESSERACT_ARGS = (
    "--oem", "1",
    "--psm", "6",
    "--dpi", "300",
    "-c", "preserve_interword_spaces=1",
)


class DesktopError(RuntimeError):
    """A desktop tool is missing, hung, or answered with something unusable.

    One exception type for the whole module, so a tool body can wrap a call in a
    single ``except DesktopError`` and turn any of it into one clear message.
    """


def _exe(argv: list[str]) -> str:
    """The binary this argv should run, as a DesktopError if there is none.

    None of these are `omarchy` commands, so they never pass through
    `execute.run` -- and the same reasoning applies to them twice over. `grim`
    and `tesseract` are ordinary names in /usr/bin with a session PATH in front
    of them.
    """
    # Imported inside the function to avoid a circular import at module load;
    # `registry.py` carries the same note.
    from .execute import NotInstalled, resolve_binary

    try:
        return resolve_binary(argv[0])
    except NotInstalled as exc:
        raise DesktopError(str(exc)) from exc


@dataclass(frozen=True)
class Capture:
    """A screenshot: the PNG itself, and the size it actually came out at."""

    #: ``bytes``, not ``str``: a PNG is binary and is not text in any encoding.
    png: bytes
    width: int
    height: int

    def as_base64(self) -> str:
        """The PNG as text, which is the only way JSON can carry it.

        Base64 re-encodes arbitrary bytes as 64 safe characters, costing about a
        third more size. ``b64encode`` returns bytes in turn, so ``.decode`` --
        safely ASCII, since those 64 characters are all it can produce.
        """
        return base64.b64encode(self.png).decode("ascii")


# ``*args`` collects any number of positional arguments into a tuple, so this is
# called as ``hyprctl("monitors")`` or ``hyprctl("dispatch", "workspace", "2")``.
def hyprctl(*args: str) -> object:
    """Run `hyprctl -j` and parse the reply.

    Returns whatever the JSON was -- Hyprland answers some queries with a list
    and others with an object -- so every caller checks with ``isinstance``
    before indexing into it.
    """
    argv = ["hyprctl", "-j", *args]
    try:
        proc = subprocess.run(
            argv,
            executable=_exe(argv),
            capture_output=True,
            text=True,
            timeout=HYPRCTL_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise DesktopError("`hyprctl` is not on PATH; is Hyprland running?") from exc
    except subprocess.TimeoutExpired as exc:
        raise DesktopError("hyprctl timed out") from exc

    if proc.returncode != 0:
        raise DesktopError(f"hyprctl {' '.join(args)} failed: {proc.stderr.strip()[:200]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise DesktopError(f"hyprctl {' '.join(args)} returned invalid JSON") from exc


def focused_monitor() -> dict[str, Any]:
    """The monitor with keyboard focus, or the first one if nothing claims it."""
    monitors = hyprctl("monitors")
    if not isinstance(monitors, list) or not monitors:
        raise DesktopError("Hyprland reported no monitors")
    for monitor in monitors:
        if monitor.get("focused"):
            return monitor
    return monitors[0]


def _grim_args(target: str, monitor: str, region: str) -> list[str]:
    """Translate a target into grim's arguments.

    `omarchy capture screenshot` is deliberately not used: it freezes the
    screen, copies to the clipboard, sends a notification, and writes a file
    into the user's Pictures directory. All four are right for a person pressing
    a key and wrong for an agent looking at the screen, which would otherwise
    litter the photo library.
    """
    if target == "region":
        if not region:
            raise DesktopError('target "region" needs a region like "0,0 800x600"')
        return ["-g", region]

    if target == "monitor":
        if not monitor:
            raise DesktopError('target "monitor" needs a monitor name; see omarchy_desktop_state')
        return ["-o", monitor]

    if target == "window":
        active = hyprctl("activewindow")
        if not isinstance(active, dict) or not active.get("address"):
            raise DesktopError("no window is focused")
        # Both arrive as two-element lists and are unpacked into a pair of
        # variables; ``or (0, 0)`` covers the field being absent or null.
        x, y = active.get("at") or (0, 0)
        w, h = active.get("size") or (0, 0)
        if w <= 0 or h <= 0:
            raise DesktopError("the focused window reported no size")
        return ["-g", f"{x},{y} {w}x{h}"]

    # "screen": the focused monitor.
    return ["-o", focused_monitor()["name"]]


def capture(
    *,
    target: str = "screen",
    monitor: str = "",
    region: str = "",
    max_width: int = DEFAULT_MAX_WIDTH,
) -> Capture:
    """Grab pixels, scaled down to something worth sending.

    On a monitor with a fractional scale, grim's `-g` geometry is in *logical*
    coordinates while the file it writes is in *physical* pixels. A 100x50
    region on a 1.25-scaled display therefore comes back as 125x62. `max_width`
    caps physical pixels, which is the number that matters for what gets sent.
    """
    args = _grim_args(target, monitor, region)

    # A named temporary rather than grim's stdout: piping straight into
    # ImageMagick hides which of the two failed when something goes wrong.
    # ``TemporaryDirectory`` used as a context manager: the directory and
    # everything in it are deleted when the ``with`` block ends, however it ends.
    # In XDG_RUNTIME_DIR because that is a per-user directory that the system
    # clears at logout, so a screenshot never lands somewhere shared.
    with tempfile.TemporaryDirectory(dir=os.environ.get("XDG_RUNTIME_DIR") or "/tmp") as tmp:
        raw = os.path.join(tmp, "capture.png")
        _run(["grim", *args, raw], GRIM_TIMEOUT_S, "grim")

        shrunk = os.path.join(tmp, "small.png")
        # `>` only shrinks: a small region is never blown up to the cap.
        _run(
            ["magick", raw, "-resize", f"{max_width}x>", "-strip", shrunk],
            GRIM_TIMEOUT_S,
            "magick",
        )
        # ``"rb"`` is read-binary: no decoding, no newline translation. The
        # bytes have to be read before the ``with`` ends and the directory goes.
        png = open(shrunk, "rb").read()
        size = _run(
            ["magick", "identify", "-format", "%w %h", shrunk], GRIM_TIMEOUT_S, "magick identify"
        )

    # ImageMagick printed "1568 882"; ``partition`` splits it into the two
    # numbers and the space between them, which is discarded as ``_``.
    width, _, height = size.partition(" ")
    return Capture(png=png, width=int(width or 0), height=int(height or 0))


def ocr(*, target: str = "screen", monitor: str = "", region: str = "", lang: str = "") -> str:
    """Read the text on screen.

    Same tesseract tuning as `omarchy capture text`, so this reads the same as
    the desktop shortcut does. That command selects a region interactively,
    which would block a tool call until a human dragged a box, so the pipeline
    is reproduced here instead of shelled out to.
    """
    args = _grim_args(target, monitor, region)
    lang = lang or os.environ.get("OMARCHY_OCR_LANGS") or "eng"

    with tempfile.TemporaryDirectory(dir=os.environ.get("XDG_RUNTIME_DIR") or "/tmp") as tmp:
        raw = os.path.join(tmp, "ocr.png")
        _run(["grim", *args, raw], GRIM_TIMEOUT_S, "grim")
        return _run(
            ["tesseract", raw, "stdout", *TESSERACT_ARGS, "-l", lang],
            OCR_TIMEOUT_S,
            "tesseract",
        )


def clipboard_read(*, mime: str = "") -> str:
    """Whatever text is on the clipboard, or "" when there is none.

    An empty clipboard is an answer rather than a failure -- see the comment on
    the exit code below.
    """
    args = ["wl-paste", "--no-newline"]
    if mime:
        args += ["--type", mime]
    try:
        proc = subprocess.run(
            args,
            executable=_exe(args),
            capture_output=True,
            text=True,
            timeout=5,
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise DesktopError("`wl-paste` is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DesktopError("wl-paste timed out") from exc

    # wl-paste exits non-zero for an empty clipboard ("Nothing is copied"), for
    # a clipboard holding only a type it cannot render as text, and for a
    # selection that has gone away. To a caller those are the same answer --
    # there is no text -- and none of them is worth failing a tool call over.
    if proc.returncode != 0:
        return ""
    return proc.stdout


def clipboard_write(text: str) -> None:
    """Put text on the clipboard.

    Two things here are not optional. Wayland has no clipboard daemon: whoever
    offers a selection must stay alive to serve it, so `wl-copy` forks a child
    that outlives the command. If that child inherits captured pipes they never
    close, and `subprocess.run` waits out its whole timeout on a copy that
    already succeeded -- reporting failure for something that worked. So its
    output goes to /dev/null rather than to pipes.

    The text goes in on stdin rather than as an argument, which keeps a large
    clipboard clear of the argument-length limit and means text beginning with a
    dash cannot be read as a flag.
    """
    try:
        proc = subprocess.run(
            ["wl-copy"],
            executable=_exe(["wl-copy"]),
            # ``input`` writes to the child's stdin and closes it. ``encode``
            # turns the string into UTF-8 bytes, which is what a pipe carries.
            input=text.encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise DesktopError("`wl-copy` is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DesktopError("wl-copy timed out") from exc
    if proc.returncode != 0:
        raise DesktopError(f"wl-copy exited {proc.returncode}")


def _run(argv: list[str], timeout_s: int, what: str) -> str:
    """Run one desktop helper and return its stdout, or raise `DesktopError`.

    ``what`` is the name to use in the message, which is not always ``argv[0]``:
    "magick identify" is more use to a reader than "magick".
    """
    try:
        proc = subprocess.run(
            argv, executable=_exe(argv), capture_output=True, text=True, timeout=timeout_s
        )
    except FileNotFoundError as exc:
        raise DesktopError(f"`{argv[0]}` is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DesktopError(f"{what} timed out") from exc
    if proc.returncode != 0:
        raise DesktopError(f"{what} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def state() -> dict[str, Any]:
    """What is on screen, compactly.

    hyprctl's own client records carry three dozen fields each, most of which a
    caller never needs; sending all of them for twenty windows buries the answer
    in the question.
    """
    monitors = hyprctl("monitors")
    workspaces = hyprctl("workspaces")
    clients = hyprctl("clients")
    active = hyprctl("activewindow")

    # One list comprehension doing three things: keep only real dicts from a
    # reply that might not be a list at all, drop unmapped (not-on-screen)
    # windows, and cut each record down to the ten fields worth sending.
    windows = [
        {
            "address": c.get("address"),
            "class": c.get("class"),
            "title": c.get("title"),
            # ``(x or {}).get(...)`` is the safe two-level lookup: if the
            # workspace field is missing or null, ``.get`` runs on an empty dict
            # and yields None instead of raising.
            "workspace": (c.get("workspace") or {}).get("name"),
            "monitor": c.get("monitor"),
            "at": c.get("at"),
            "size": c.get("size"),
            "floating": bool(c.get("floating")),
            "fullscreen": bool(c.get("fullscreen")),
            "pid": c.get("pid"),
        }
        for c in (clients if isinstance(clients, list) else [])
        if c.get("mapped", True)
    ]

    # Counted from the windows actually listed above, not taken from
    # hyprctl's own per-workspace figure. The two disagree -- Hyprland counts
    # a group as one -- and reporting both invites a reader to treat the
    # smaller one as a reason to doubt the list.
    per_workspace: dict[str, int] = {}
    for window in windows:
        name = window["workspace"]
        per_workspace[name] = per_workspace.get(name, 0) + 1

    return {
        "monitors": [
            {
                "name": m.get("name"),
                "resolution": f"{m.get('width')}x{m.get('height')}",
                "position": f"{m.get('x')},{m.get('y')}",
                "scale": m.get("scale"),
                "refresh_hz": round(m.get("refreshRate") or 0, 1),
                "active_workspace": (m.get("activeWorkspace") or {}).get("name"),
                "focused": bool(m.get("focused")),
            }
            for m in (monitors if isinstance(monitors, list) else [])
        ],
        "workspaces": sorted(
            (
                {
                    "id": w.get("id"),
                    "name": w.get("name"),
                    "monitor": w.get("monitor"),
                    "windows": per_workspace.get(w.get("name"), 0),
                }
                for w in (workspaces if isinstance(workspaces, list) else [])
            ),
            # Sorting by a pair puts every workspace with an id first, in id
            # order, and any without one at the end. Sorting on the id alone
            # would raise, because None and an int cannot be compared.
            key=lambda w: (w["id"] is None, w["id"]),
        ),
        "windows": windows,
        "focused_window": (
            {
                "address": active.get("address"),
                "class": active.get("class"),
                "title": active.get("title"),
                "workspace": (active.get("workspace") or {}).get("name"),
            }
            if isinstance(active, dict) and active.get("address")
            else None
        ),
    }
