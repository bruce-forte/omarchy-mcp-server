"""The things that get asked for constantly.

None of these do anything `omarchy_run` could not. They exist because a search
round trip before every volume change is a bad trade, and because one tool with
an action enum is a smaller thing for a model to hold than eight routes it has
to look up first.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field, create_model

from .. import consent, execute, gate, registry, resolve, shell
from ..settings import Settings
from ..stats import Stats
from ._shared import offload, run_route
from .catalogue import Catalogue

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

#: How many themes an elicitation form offers before the enum is dropped and the
#: field becomes free text. An enum is a picker in a client that renders one;
#: past a certain length it is a wall, and the resolver refuses a wrong name
#: with near misses anyway.
MAX_CHOICES = 60

MEDIA_ACTIONS = (
    "playPause", "play", "pause", "next", "previous", "status",
    "sourceNext", "sourcePrevious",
)


def register(tools: Catalogue, settings: Settings, log, stats: Stats) -> None:
    """Declare the seven convenience tools in ``tools``.

    Each one is a small ``action`` switch over a handful of registry routes:
    validate what was asked for, refuse clearly if it makes no sense, otherwise
    hand the route to `run` below.
    """

    # A local shorthand for `_shared.run_route`, closing over the four arguments
    # every call in this module would otherwise repeat.
    async def run(route, args, tool, ctx, **kw):
        # One snapshot per call: a reload between two calls is seen, a reload
        # during one is not.
        return await run_route(
            route,
            args,
            config=settings.current,
            perms=settings.permissions,
            unreviewed=settings.unreviewed,
            stats=stats,
            log=log,
            tool=tool,
            ctx=ctx,
            **kw,
        )

    # ---------------------------------------------------------------- theme

    @tools.tool(
        name="omarchy_theme",
        title="Get, list, or set the theme",
        description=(
            "Read the current Omarchy theme, list the installed ones, or apply one. "
            "Applying a theme restyles the whole desktop -- shell, terminals, and "
            "GTK apps -- so confirm the name against `list` rather than guessing it. "
            'Calling action="set" with no name asks the user to pick one, if their '
            "client can be asked; otherwise it is an error and you should call "
            'action="list" first.'
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def omarchy_theme(action: str = "current", name: str = "", ctx: Context = None) -> str:
        """Read, list, or apply the theme, depending on ``action``."""
        if action == "current":
            return await run("omarchy theme current", [], "omarchy_theme", ctx)
        if action == "list":
            return await run("omarchy theme list", [], "omarchy_theme", ctx)
        if action == "set":
            if not name:
                # No theme named. Ask the person which one, if their client can
                # be asked; otherwise this stays the error it always was.
                picked = await _pick_theme(ctx, settings.permissions, log)
                if isinstance(picked, _Refused):
                    return json.dumps(picked.payload, indent=2)
                name = picked.name
            return await run("omarchy theme set", [name], "omarchy_theme", ctx)
        return json.dumps({"error": 'action must be current, list, or set'}, indent=2)

    @tools.tool(
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
        """Read, cycle, or set the desktop background."""
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

    @tools.tool(
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
        """Volume, mute, microphone mute, or output switching."""
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

    @tools.tool(
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
        """Display or keyboard brightness. An empty ``value`` reads it instead."""
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

    @tools.tool(
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
        """Drive the shell's media target directly.

        The one tool here that does not go through `run`: this is an IPC call
        rather than a registry route, so there is no `Command` for the gate to
        classify. It is read-mostly playback control on the running shell.
        """
        if action not in MEDIA_ACTIONS:
            return json.dumps(
                {"error": f"action must be one of {', '.join(MEDIA_ACTIONS)}"}, indent=2
            )

        with stats.call("omarchy_media") as rec:
            cfg = settings.current
            rec.route = f"media.{action}"
            argv = shell.call_argv("media", action, [])
            result = await offload(
                execute.run,
                argv,
                timeout_ms=cfg.timeout_ms,
                max_output_b=cfg.max_output_b,
            )
            rec.exit = result.exit_code
            rec.timed_out = result.timed_out
            log.info("omarchy_media %s exit=%s", action, result.exit_code)
            return json.dumps({"command": execute.quote(argv), **result.as_dict()}, indent=2)

    # -------------------------------------------------------------- toggles

    @tools.tool(
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
        """Flip one of the named desktop flags on, off, or over."""
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

    @tools.tool(
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
        """Open a browser, editor, terminal, web app, or file manager."""
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
    # ``findall`` returns every match rather than just the first. The pattern is
    # "a lowercase letter, then any number of lowercase letters or dashes",
    # which picks the words out of an argument spec like "on|off|toggle".
    return set(re.findall(r"[a-z][a-z-]*", cmd.args))


def state_argument(route: str, state: str) -> str | None:
    """The word this route wants for `state`, or None if it has no such form.

    An empty string means the route takes no argument, which for a toggle is
    how it spells "toggle".
    """
    accepted = accepted_states(route)
    # In preference order, so a route accepting both "off" and "allow-idle"
    # gets the more obvious of the two.
    for word in STATE_WORDS[state]:
        if word in accepted:
            return word
    if state == "toggle":
        # A route with no arguments at all toggles when called bare.
        return ""
    return None


def _is_step(value: str) -> bool:
    """Whether ``value`` is a signed step like ``+10`` or ``-5``."""
    # ``isdigit()`` on the rest is what rejects "+ten" and "+1.5"; the length
    # check is what stops a bare "+" reaching ``value[1:]`` as an empty string,
    # which ``isdigit`` calls False anyway but less obviously.
    return len(value) > 1 and value[0] in "+-" and value[1:].isdigit()


# ------------------------------------------------- asking which one, in a form
#
# The other half of elicitation. `gate.py` uses it to ask *may this run*; this
# uses it to ask *which one did you mean*, which is what form mode is actually
# for -- the question carries a schema, and the answer comes back typed.
#
# Three properties are deliberate, and the first is the one that matters:
#
# **It decides nothing.** The chosen name is handed straight back to the same
# `run_route` an explicit name would have taken, so the tier, the rules, the
# resolver and the approval prompt all still apply to it. Picking a theme from a
# list is not consent to switch to it, and this must never become a second door
# into the executor.
#
# **A client that cannot be asked keeps the old error.** Claude Code negotiates
# a protocol with no back-channel (F23), so for it nothing changes: the call
# still comes back saying a name is needed.
#
# **No answer runs nothing.** It goes through `consent.ask`, so the deadline,
# the decline and the disconnect are the same six outcomes as everywhere else --
# and the same rule that only an accept proceeds.


@dataclass(frozen=True)
class _Picked:
    """A theme the person chose from the form."""

    name: str


@dataclass(frozen=True)
class _Refused:
    """No name to run with, and what the agent is told instead."""

    payload: dict


def _needs_a_name(extra: str = "") -> _Refused:
    """The refusal for a `set` with nothing to set, as it always read."""
    return _Refused({"error": ('action "set" needs a theme name' + extra)})


#: An args spec that is exactly one placeholder, which is the only shape that
#: names a single value: `<theme-name>`, `<path-to-image>`.
_ONE_PLACEHOLDER = re.compile(r"^<([a-z][a-z0-9-]*)>$")


def _wording(route: str, thing: str) -> tuple[str, str]:
    """The form's message and field description, in Omarchy's own words.

    Both are read from the registry entry for ``route`` rather than written
    here: ``summary`` is how Omarchy describes the command -- "Apply an Omarchy
    theme" -- and ``args`` is what the command calls its parameter,
    ``<theme-name>``. So the question a person is asked says what
    ``omarchy theme set --help`` says, and goes on saying it after upstream
    rewords the command. That is the same reason nothing else here keeps a
    catalogue: the registry is the source of truth, and this is one more thing
    read from it rather than duplicated beside it.

    ``thing`` is what to call the value when the registry cannot say -- a route
    that has been renamed away, or one whose summary is empty. A missing
    summary must not produce a form with no question on it.
    """
    cmd = registry.get(route)
    summary = (cmd.summary if cmd else "").strip().rstrip(".")
    # `<theme-name>` -> "theme name". Matched whole rather than stripped of
    # brackets, so that an args spec naming anything other than exactly one
    # placeholder -- `[--monitor NAME] [value]`, an alternation, a flag list --
    # fails to match and falls back instead of being mangled into a label.
    match = _ONE_PLACEHOLDER.match((cmd.args if cmd else "").strip())
    label = match.group(1).replace("-", " ") if match else thing
    message = f"{summary}. Which one?" if summary else f"Which {thing} should I use?"
    return message, f"The {label} to apply"


def _choice_model(themes: list[str], description: str):
    """A one-field pydantic model whose field is an enum of ``themes``.

    Built per call because the choices are whatever is installed right now. A
    `Literal` renders as a JSON Schema ``enum``, which is what makes a client
    show a picker rather than a text box; past `MAX_CHOICES` the enum is
    dropped, because a hundred-entry dropdown is worse than typing.
    """
    described = Field(description=description)
    if 0 < len(themes) <= MAX_CHOICES:
        field = (Literal[tuple(themes)], described)
    else:
        field = (str, described)
    return create_model("ThemeChoice", name=field)


async def _pick_theme(ctx, perms, log) -> _Picked | _Refused:
    """Ask the person which theme, and return it. Never runs anything."""
    if not gate.can_elicit(ctx):
        # The client declared no elicitation, or its transport cannot carry a
        # server-initiated request. Either way there is nobody to ask.
        return _needs_a_name()

    try:
        themes = await offload(resolve._themes)
    except resolve.Unresolvable as exc:
        # The theme list is the form's contents. Without it there is no form,
        # and guessing is what `resolve.py` exists not to do.
        log.info("cannot offer a theme list: %s", exc.message)
        return _needs_a_name(f"; the installed themes could not be listed ({exc.message})")

    if not themes:
        return _needs_a_name("; no themes appear to be installed")

    message, description = _wording("omarchy theme set", "theme")
    model = _choice_model(themes, description)
    # The same deadline a consent question gets, deliberately: a form left
    # unanswered is a tool call parked on somebody who walked away, which is the
    # thing `askTimeoutSeconds` bounds. It is the user's own setting, so
    # somebody who finds 60s tight for reading a list can raise it. A late
    # answer is ignored rather than acted on, as everywhere else here.
    answer = await consent.ask(
        lambda: ctx.elicit(message, model),
        what="which theme to switch to",
        timeout_s=perms.ask_timeout_s,
        log=log,
    )
    if not answer.accepted:
        log.info("theme choice %s", answer.outcome.value)
        return _Refused(
            {
                "error": answer.reason or "The user did not choose a theme.",
                "consent": answer.outcome.value,
            }
        )

    chosen = getattr(answer.data, "name", "")
    if not isinstance(chosen, str) or not chosen.strip():
        # An accept whose content does not fit the schema this server sent. The
        # SDK validates it, so this is the belt to that braces -- and an empty
        # name would otherwise reach `omarchy theme set` as no argument at all,
        # which opens the interactive picker on the user's screen.
        return _needs_a_name("; the client accepted without choosing one")

    log.info("theme chosen by elicitation: %r", chosen)
    return _Picked(chosen.strip())
