"""Tools for the things `omarchy_run` structurally cannot do.

A tool earns a slot here only if the generic runner cannot produce its result:
because the result is not text (a screenshot), or because the data does not come
from the `omarchy` command at all (Hyprland's window layout, the clipboard).
Everything else stays in `omarchy_run`.
"""

from __future__ import annotations

import json

from mcp.types import ImageContent, TextContent, ToolAnnotations

from .. import desktop
from ..config import Config
from ..stats import Stats

TARGETS = "screen (the focused monitor), window (the focused window), monitor, or region"


def register(mcp, config: Config, log, stats: Stats) -> None:
    disabled = set(config.disabled_tools)

    def enabled(name: str) -> bool:
        return name not in disabled

    if enabled("omarchy_screenshot"):

        @mcp.tool(
            name="omarchy_screenshot",
            title="Look at the screen",
            description=(
                "Capture the screen and return it as an image, so you can see what is "
                f"actually there. `target` is one of: {TARGETS}. For `monitor`, pass a "
                "monitor name from omarchy_desktop_state. For `region`, pass geometry "
                "like '0,0 800x600'. Images are scaled down before being returned; "
                "nothing is saved to disk and the clipboard is not touched."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=False,
                openWorldHint=False,
            ),
        )
        def omarchy_screenshot(
            target: str = "screen",
            monitor: str = "",
            region: str = "",
            max_width: int = desktop.DEFAULT_MAX_WIDTH,
        ) -> list:
            stats.record("omarchy_screenshot", route=target)
            max_width = max(64, min(max_width, 3840))
            try:
                shot = desktop.capture(
                    target=target, monitor=monitor, region=region, max_width=max_width
                )
            except desktop.DesktopError as exc:
                stats.record("omarchy_screenshot", ok=False)
                return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

            log.info("screenshot target=%s %dx%d", target, shot.width, shot.height)
            return [
                ImageContent(type="image", data=shot.as_base64(), mimeType="image/png"),
                TextContent(
                    type="text",
                    text=f"{shot.width}x{shot.height}, target={target}",
                ),
            ]

    if enabled("omarchy_desktop_state"):

        @mcp.tool(
            name="omarchy_desktop_state",
            title="What is on screen",
            description=(
                "Report the desktop layout from Hyprland: monitors, workspaces, every "
                "open window with its class, title, position, and workspace, and which "
                "window is focused. This is not an Omarchy command and is not reachable "
                "through omarchy_run. Use it before acting on 'the current window' or "
                "'the other monitor'."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                openWorldHint=False,
            ),
        )
        def omarchy_desktop_state() -> str:
            stats.record("omarchy_desktop_state")
            try:
                return json.dumps(desktop.state(), indent=2)
            except desktop.DesktopError as exc:
                stats.record("omarchy_desktop_state", ok=False)
                return json.dumps({"error": str(exc)}, indent=2)

    if enabled("omarchy_screen_text"):

        @mcp.tool(
            name="omarchy_screen_text",
            title="Read the text on screen",
            description=(
                "Extract text from the screen with OCR. Cheaper than a screenshot when "
                "you only need what something says, and it works on text inside images "
                f"and terminals. `target` is one of: {TARGETS}."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=False,
                openWorldHint=False,
            ),
        )
        def omarchy_screen_text(
            target: str = "screen", monitor: str = "", region: str = "", lang: str = ""
        ) -> str:
            stats.record("omarchy_screen_text", route=target)
            try:
                text = desktop.ocr(target=target, monitor=monitor, region=region, lang=lang)
            except desktop.DesktopError as exc:
                stats.record("omarchy_screen_text", ok=False)
                return json.dumps({"error": str(exc)}, indent=2)

            capped, truncated = _cap(text, config.max_output_b)
            return json.dumps({"text": capped, "truncated": truncated}, indent=2)

    if enabled("omarchy_clipboard_read"):

        @mcp.tool(
            name="omarchy_clipboard_read",
            title="Read the clipboard",
            description=(
                "Return the current clipboard contents as text. Reading the clipboard is "
                "not an Omarchy command, so this is not reachable through omarchy_run. "
                "An empty clipboard returns an empty string rather than an error."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                openWorldHint=False,
            ),
        )
        def omarchy_clipboard_read(mime: str = "") -> str:
            stats.record("omarchy_clipboard_read")
            try:
                text = desktop.clipboard_read(mime=mime)
            except desktop.DesktopError as exc:
                stats.record("omarchy_clipboard_read", ok=False)
                return json.dumps({"error": str(exc)}, indent=2)

            capped, truncated = _cap(text, config.max_output_b)
            return json.dumps({"text": capped, "truncated": truncated}, indent=2)

    if enabled("omarchy_clipboard_write"):

        @mcp.tool(
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
        def omarchy_clipboard_write(text: str) -> str:
            stats.record("omarchy_clipboard_write")
            try:
                desktop.clipboard_write(text)
            except desktop.DesktopError as exc:
                stats.record("omarchy_clipboard_write", ok=False)
                return json.dumps({"error": str(exc)}, indent=2)
            return json.dumps({"ok": True, "bytes": len(text.encode())})


def _cap(text: str, limit: int) -> tuple[str, bool]:
    from ..execute import cap

    return cap(text, limit)
