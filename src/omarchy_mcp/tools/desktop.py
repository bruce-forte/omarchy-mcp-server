"""Tools for the things `omarchy_run` structurally cannot do.

A tool earns a slot here only if the generic runner cannot produce its result:
because the result is not text (a screenshot), or because the data does not come
from the `omarchy` command at all (Hyprland's window layout, the clipboard).
Everything else stays in `omarchy_run`.
"""

from __future__ import annotations

import json

from mcp.types import ImageContent, TextContent, ToolAnnotations

from .. import desktop, resolve
from ..settings import Settings
from ..stats import Stats
from ._shared import UNTRUSTED, threaded
from .catalogue import Catalogue

TARGETS = "screen (the focused monitor), window (the focused window), monitor, or region"


def register(tools: Catalogue, settings: Settings, log, stats: Stats) -> None:
    @tools.tool(
        name="omarchy_screenshot",
        title="Look at the screen",
        description=(
            "Capture the screen and return it as an image, so you can see what is "
            f"actually there. `target` is one of: {TARGETS}. For `monitor`, pass a "
            "monitor name from omarchy_desktop_state. For `region`, pass geometry "
            "like '0,0 800x600'. Images are scaled down before being returned; "
            "nothing is saved to disk and the clipboard is not touched." + UNTRUSTED
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @threaded
    def omarchy_screenshot(
        target: str = "screen",
        monitor: str = "",
        region: str = "",
        max_width: int = desktop.DEFAULT_MAX_WIDTH,
    ) -> list:
        with stats.call("omarchy_screenshot") as rec:
            rec.route = target
            max_width = max(64, min(max_width, 3840))
            try:
                named = _monitor(target, monitor)
                shot = desktop.capture(
                    target=target,
                    monitor=named.value if named else monitor,
                    region=region,
                    max_width=max_width,
                )
            except resolve.Unresolvable as exc:
                rec.outcome = "error"
                return [TextContent(type="text", text=json.dumps(exc.as_dict()))]
            except desktop.DesktopError as exc:
                rec.outcome = "error"
                return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]
            if named:
                rec.target = named.label

            where = f", {named.label}" if named else ""
            log.info(
                "screenshot target=%s monitor=%r %dx%d",
                target,
                named.label if named else None,
                shot.width,
                shot.height,
            )
            return [
                ImageContent(type="image", data=shot.as_base64(), mimeType="image/png"),
                TextContent(
                    type="text",
                    text=f"{shot.width}x{shot.height}, target={target}{where}",
                ),
            ]

    @tools.tool(
        name="omarchy_desktop_state",
        title="What is on screen",
        description=(
            "Report the desktop layout from Hyprland: monitors, workspaces, every "
            "open window with its class, title, position, and workspace, and which "
            "window is focused. This is not an Omarchy command and is not reachable "
            "through omarchy_run. Use it before acting on 'the current window' or "
            "'the other monitor'." + UNTRUSTED
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @threaded
    def omarchy_desktop_state() -> str:
        with stats.call("omarchy_desktop_state") as rec:
            try:
                return json.dumps(desktop.state(), indent=2)
            except desktop.DesktopError as exc:
                rec.outcome = "error"
                return json.dumps({"error": str(exc)}, indent=2)

    @tools.tool(
        name="omarchy_screen_text",
        title="Read the text on screen",
        description=(
            "Extract text from the screen with OCR. Cheaper than a screenshot when "
            "you only need what something says, and it works on text inside images "
            f"and terminals. `target` is one of: {TARGETS}." + UNTRUSTED
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @threaded
    def omarchy_screen_text(
        target: str = "screen", monitor: str = "", region: str = "", lang: str = ""
    ) -> str:
        with stats.call("omarchy_screen_text") as rec:
            rec.route = target
            try:
                named = _monitor(target, monitor)
                text = desktop.ocr(
                    target=target,
                    monitor=named.value if named else monitor,
                    region=region,
                    lang=lang,
                )
            except resolve.Unresolvable as exc:
                rec.outcome = "error"
                return json.dumps(exc.as_dict(), indent=2)
            except desktop.DesktopError as exc:
                rec.outcome = "error"
                return json.dumps({"error": str(exc)}, indent=2)
            if named:
                rec.target = named.label

            # The extracted text is never logged: it is the contents of the
            # screen, and a record of that is a different thing entirely.
            capped, truncated = _cap(text, settings.current.max_output_b)
            payload: dict[str, object] = {"text": capped, "truncated": truncated}
            if named:
                payload["target"] = named.label
            return json.dumps(payload, indent=2)

    @tools.tool(
        name="omarchy_clipboard_read",
        title="Read the clipboard",
        description=(
            "Return the current clipboard contents as text. Reading the clipboard is "
            "not an Omarchy command, so this is not reachable through omarchy_run. "
            "An empty clipboard returns an empty string rather than an error." + UNTRUSTED
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @threaded
    def omarchy_clipboard_read(mime: str = "") -> str:
        with stats.call("omarchy_clipboard_read") as rec:
            try:
                text = desktop.clipboard_read(mime=mime)
            except desktop.DesktopError as exc:
                rec.outcome = "error"
                return json.dumps({"error": str(exc)}, indent=2)

            # What was on the clipboard is not logged, for the same reason OCR
            # text is not: it is the user's data, not the agent's action.
            capped, truncated = _cap(text, settings.current.max_output_b)
            return json.dumps({"text": capped, "truncated": truncated}, indent=2)

    @tools.tool(
        name="omarchy_clipboard_write",
        title="Put text on the clipboard",
        description=(
            "Replace the clipboard contents with the given text. This overwrites "
            "whatever the user had copied, which they will not be expecting, so say "
            "what you are putting there."
        ),
        # Not read-only, and destructive in the sense that matters: it
        # discards something the user put there deliberately.
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @threaded
    def omarchy_clipboard_write(text: str) -> str:
        with stats.call("omarchy_clipboard_write") as rec:
            # Logged, truncated: this one *is* the action. What the agent
            # put on the clipboard is the thing a user would come back to
            # the log to find out.
            rec.args = (text,)
            try:
                desktop.clipboard_write(text)
            except desktop.DesktopError as exc:
                rec.outcome = "error"
                return json.dumps({"error": str(exc)}, indent=2)
            return json.dumps({"ok": True, "bytes": len(text.encode())})


def _monitor(target: str, monitor: str) -> resolve.Target | None:
    """Resolve a named output before grim is asked for it.

    These tools do not go through `run_route`, so they call the resolver
    themselves rather than let grim fail on a name that was never connected.
    A monitor name is only meaningful for `target="monitor"`; anywhere else it
    is ignored, and resolving it would refuse a call over an argument that has
    no effect.
    """
    if target != "monitor" or not monitor:
        return None
    return resolve.monitor(monitor)


def _cap(text: str, limit: int) -> tuple[str, bool]:
    from ..execute import cap

    return cap(text, limit)
