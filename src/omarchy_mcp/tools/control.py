"""The things that get asked for constantly.

None of these do anything `omarchy_run` could not. They exist because a search
round trip before every volume change is a bad trade, and because one tool with
an action enum is a smaller thing for a model to hold than eight routes it has
to look up first.
"""

from __future__ import annotations

import json
import re

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from .. import execute, registry, shell
from ..config import Config
from ..stats import Stats
from ._shared import enabled, offload, run_route

#: Flags worth a first-class tool, mapped to the route that toggles them.
#: Everything else is still reachable with `omarchy toggle <flag>`.
TOGGLES: dict[str, str] = {
    "idle": "omarchy toggle idle",
    "nightlight": "omarchy toggle nightlight",
    "do_not_disturb": "omarchy toggle notification silencing",
    "bar": "omarchy toggle bar",
    "touchpad": "omarchy toggle touchpad",
    "touchscreen": "omarchy toggle touchscreen",
    "screensaver": "omarchy toggle screensaver",
}

#: What each state is called, in preference order. These routes disagree:
#: `bar` takes on/off, `idle` takes stay-awake/allow-idle, and
#: `screensaver` and `notification silencing` take nothing at all.
STATE_WORDS: dict[str, tuple[str, ...]] = {
    "on": ("on", "stay-awake"),
    "off": ("off", "allow-idle"),
    "toggle": ("toggle",),
    "status": ("status",),
}

MEDIA_ACTIONS = (
    "playPause", "play", "pause", "next", "previous", "status",
    "sourceNext", "sourcePrevious",
)


def register(mcp, config: Config, log, stats: Stats) -> None:
    async def run(route, args, tool, ctx, **kw):
        return await run_route(
            route, args, config=config, stats=stats, log=log, tool=tool, ctx=ctx, **kw
        )

    # ---------------------------------------------------------------- theme

    if enabled(config, "omarchy_theme"):

        @mcp.tool(
            name="omarchy_theme",
            title="Get, list, or set the theme",
            description=(
                "Read the current Omarchy theme, list the installed ones, or apply one. "
                "Applying a theme restyles the whole desktop -- shell, terminals, and "
                "GTK apps -- so confirm the name against `list` rather than guessing it."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def omarchy_theme(action: str = "current", name: str = "", ctx: Context = None) -> str:
            if action == "current":
                return await run("omarchy theme current", [], "omarchy_theme", ctx)
            if action == "list":
                return await run("omarchy theme list", [], "omarchy_theme", ctx)
            if action == "set":
                if not name:
                    return json.dumps({"error": 'action "set" needs a theme name'}, indent=2)
                return await run("omarchy theme set", [name], "omarchy_theme", ctx)
            return json.dumps({"error": 'action must be current, list, or set'}, indent=2)

    if enabled(config, "omarchy_background"):

        @mcp.tool(
            name="omarchy_background",
            title="Get, cycle, or set the background",
            description=(
                "Read the current desktop background, cycle to the next one in the "
                "current theme, or set a specific image by absolute path."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                openWorldHint=False,
            ),
        )
        async def omarchy_background(action: str = "current", path: str = "", ctx: Context = None) -> str:
            if action == "current":
                return await run("omarchy theme bg current", [], "omarchy_background", ctx)
            if action == "next":
                return await run("omarchy theme bg next", [], "omarchy_background", ctx)
            if action == "set":
                if not path:
                    return json.dumps(
                        {"error": 'action "set" needs an absolute image path'}, indent=2
                    )
                return await run("omarchy theme bg set", [path], "omarchy_background", ctx)
            return json.dumps({"error": "action must be current, next, or set"}, indent=2)

    # ---------------------------------------------------------------- audio

    if enabled(config, "omarchy_audio"):

        @mcp.tool(
            name="omarchy_audio",
            title="Volume, mute, and output switching",
            description=(
                "Adjust output volume, toggle output or microphone mute, or switch "
                "between audio outputs. `level` accepts 'raise', 'lower', or a signed "
                "step like '+10' or '-5'. Volume changes show the Omarchy OSD, so the "
                "user sees what happened."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                openWorldHint=False,
            ),
        )
        async def omarchy_audio(action: str = "volume", level: str = "raise", ctx: Context = None) -> str:
            if action == "volume":
                if level not in ("raise", "lower") and not _is_step(level):
                    return json.dumps(
                        {"error": "level must be 'raise', 'lower', or a signed step like '+10'"},
                        indent=2,
                    )
                return await run("omarchy audio output volume", [level], "omarchy_audio", ctx)
            if action == "mute":
                return await run(
                    "omarchy audio output volume", ["mute-toggle"], "omarchy_audio", ctx
                )
            if action == "mic_mute":
                return await run("omarchy audio input mute", [], "omarchy_audio", ctx)
            if action == "switch_output":
                return await run("omarchy audio output switch", [], "omarchy_audio", ctx)
            return json.dumps(
                {"error": "action must be volume, mute, mic_mute, or switch_output"}, indent=2
            )

    if enabled(config, "omarchy_brightness"):

        @mcp.tool(
            name="omarchy_brightness",
            title="Screen and keyboard brightness",
            description=(
                "Show or change display brightness, or step the keyboard backlight. For "
                "the display, `value` is a percentage like '50%', a relative step like "
                "'+10%' or '10%-', or 'on'/'off'. Omit it to read the current level."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                openWorldHint=False,
            ),
        )
        async def omarchy_brightness(
            target: str = "display", value: str = "", monitor: str = "", ctx: Context = None
        ) -> str:
            if target == "display":
                args: list[str] = []
                if monitor:
                    args += ["--monitor", monitor]
                if value:
                    args.append(value)
                return await run("omarchy brightness display", args, "omarchy_brightness", ctx)
            if target == "keyboard":
                if not value:
                    return json.dumps(
                        {"error": "keyboard brightness needs up, down, cycle, off, or restore"},
                        indent=2,
                    )
                return await run("omarchy brightness keyboard", [value], "omarchy_brightness", ctx)
            return json.dumps({"error": "target must be display or keyboard"}, indent=2)

    # ---------------------------------------------------------------- media

    if enabled(config, "omarchy_media"):

        @mcp.tool(
            name="omarchy_media",
            title="Control media playback",
            description=(
                "Play, pause, skip, or report what is playing. This talks to the running "
                "shell rather than to a command, so it follows whichever player Omarchy "
                "currently considers active. `status` reports the track without changing "
                "anything."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                openWorldHint=False,
            ),
        )
        async def omarchy_media(action: str = "status") -> str:
            if action not in MEDIA_ACTIONS:
                return json.dumps(
                    {"error": f"action must be one of {', '.join(MEDIA_ACTIONS)}"}, indent=2
                )

            with stats.call("omarchy_media") as rec:
                rec.route = f"media.{action}"
                argv = shell.call_argv("media", action, [])
                result = await offload(
                    execute.run,
                    argv,
                    timeout_ms=config.timeout_ms,
                    max_output_b=config.max_output_b,
                )
                rec.exit = result.exit_code
                rec.timed_out = result.timed_out
                log.info("omarchy_media %s exit=%s", action, result.exit_code)
                return json.dumps({"command": execute.quote(argv), **result.as_dict()}, indent=2)

    # -------------------------------------------------------------- toggles

    if enabled(config, "omarchy_toggle"):

        @mcp.tool(
            name="omarchy_toggle",
            title="Flip a desktop feature on or off",
            description=(
                "Turn a desktop feature on, off, or over: "
                + ", ".join(sorted(TOGGLES))
                + ". `state` is 'toggle', 'on', or 'off'. Other Omarchy flags are "
                "reachable through omarchy_run with `omarchy toggle <flag>`."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                openWorldHint=False,
            ),
        )
        async def omarchy_toggle(feature: str, state: str = "toggle", ctx: Context = None) -> str:
            route = TOGGLES.get(feature)
            if route is None:
                return json.dumps(
                    {
                        "error": f"unknown feature {feature!r}",
                        "known": sorted(TOGGLES),
                        "hint": "Other flags: omarchy_run with `omarchy toggle <flag>`.",
                    },
                    indent=2,
                )
            if state not in STATE_WORDS:
                return json.dumps(
                    {"error": f"state must be one of {', '.join(STATE_WORDS)}"}, indent=2
                )

            word = state_argument(route, state)
            if word is None:
                return json.dumps(
                    {
                        "error": f"`{route}` cannot be set to {state!r}; it only toggles.",
                        "accepts": sorted(accepted_states(route)) or ["toggle"],
                    },
                    indent=2,
                )

            return await run(route, [word] if word else [], "omarchy_toggle", ctx)

    # --------------------------------------------------------------- launch

    if enabled(config, "omarchy_launch"):

        @mcp.tool(
            name="omarchy_launch",
            title="Open something",
            description=(
                "Open a URL in the browser, a path in the editor, a terminal, or a URL "
                "as a web app. These start a window and return immediately rather than "
                "waiting for it to be closed."
            ),
            annotations=ToolAnnotations(
                readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                openWorldHint=True,
            ),
        )
        async def omarchy_launch(what: str, target: str = "", ctx: Context = None) -> str:
            routes = {
                "browser": "omarchy launch browser",
                "editor": "omarchy launch editor",
                "terminal": "omarchy launch terminal",
                "webapp": "omarchy launch webapp",
                "files": "omarchy launch nautilus",
            }
            route = routes.get(what)
            if route is None:
                return json.dumps(
                    {"error": f"unknown target {what!r}", "known": sorted(routes)}, indent=2
                )
            if what in ("editor", "webapp") and not target:
                return json.dumps({"error": f"{what} needs a path or URL"}, indent=2)

            return await run(
                route, [target] if target else [], "omarchy_launch", ctx, detach=True
            )


def accepted_states(route: str) -> set[str]:
    """Which state words a toggle route accepts, read from the registry.

    These routes are not consistent -- `bar` takes on/off, `idle` takes
    stay-awake/allow-idle, `screensaver` takes nothing -- and hardcoding that
    would be a table to keep in step with Omarchy by hand. The registry already
    records each one's arguments, so it is read instead.
    """
    cmd = registry.get(route)
    if cmd is None:
        return set()
    return set(re.findall(r"[a-z][a-z-]*", cmd.args))


def state_argument(route: str, state: str) -> str | None:
    """The word this route wants for `state`, or None if it has no such form.

    An empty string means the route takes no argument, which for a toggle is
    how it spells "toggle".
    """
    accepted = accepted_states(route)
    for word in STATE_WORDS[state]:
        if word in accepted:
            return word
    if state == "toggle":
        # A route with no arguments at all toggles when called bare.
        return ""
    return None


def _is_step(value: str) -> bool:
    return len(value) > 1 and value[0] in "+-" and value[1:].isdigit()
