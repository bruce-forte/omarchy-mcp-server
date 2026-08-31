import QtQuick
import Quickshell
import Quickshell.Io

// Supervises the MCP daemon.
//
// The daemon cannot live inside the shell: it is an HTTP server that runs
// arbitrary Omarchy commands, and QML is the wrong place for either. So the
// plugin owns it as a child process. That is not merely convenient -- being a
// child of omarchy-shell is what gives the daemon WAYLAND_DISPLAY,
// HYPRLAND_INSTANCE_SIGNATURE and DBUS_SESSION_BUS_ADDRESS from the live
// session. A systemd unit would have to import them and would race the session
// at boot.
//
// The daemon prints one JSON line to stdout whenever its state changes, and
// logs to stderr. State is mirrored into a file because Omarchy routes
// inter-plugin calls to panels and overlays but not to services, so the bar
// widget cannot ask this object anything.
Item {
  id: root

  property var shell: null

  readonly property string pluginId: "io.github.bruce-forte.mcp-server"
  readonly property string pluginDir: Qt.resolvedUrl(".").toString().replace("file://", "")
  readonly property string statePath: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/omarchy-mcp.state"

  // What the daemon last told us, and what we last observed ourselves.
  property string phase: "starting"      // starting | building | listening | failed | stopped
  property int    port: 8765
  property string lastError: ""
  property bool   serving: false          // proven by /health, not assumed from a pid
  property int    calls: 0                // tool calls served, from /health
  property string lastTool: ""

  property bool   wantRunning: true
  property int    failures: 0

  // A daemon that dies immediately -- a broken venv, a syntax error -- must not
  // be respawned in a tight loop. Back off, and cap the delay so a transient
  // failure still recovers without a shell restart.
  readonly property int backoffMs: Math.min(1000 * Math.pow(2, Math.min(failures, 6)), 60000)

  function start() {
    if (daemon.running)
      return
    wantRunning = true
    phase = "starting"
    daemon.running = true
  }

  function stop() {
    wantRunning = false
    daemon.running = false
    phase = "stopped"
    serving = false
    writeState()
  }

  function restart() {
    stop()
    restartTimer.restart()
  }

  function writeState() {
    stateFile.setText(JSON.stringify({
      phase: root.phase,
      serving: root.serving,
      port: root.port,
      calls: root.calls,
      lastTool: root.lastTool,
      error: root.lastError,
      pid: daemon.processId || 0
    }))
  }

  onPhaseChanged: writeState()
  onServingChanged: writeState()
  onCallsChanged: writeState()

  Component.onCompleted: start()

  Process {
    id: daemon
    command: [root.pluginDir + "bin/omarchy-mcpd"]
    running: false

    stdout: SplitParser {
      // One JSON object per line, emitted on every state change.
      onRead: function (line) {
        if (!line)
          return
        try {
          const frame = JSON.parse(line)
          if (frame.state)
            root.phase = frame.state
          if (frame.port)
            root.port = frame.port
          root.lastError = frame.error || ""
          // Probe immediately rather than waiting out an interval: otherwise
          // the widget claims the server is down for ten seconds every start.
          if (frame.state === "listening" && !health.running)
            health.running = true
        } catch (e) {
          // Not a state line; the wrapper's own output goes to stderr, so this
          // is unexpected but harmless.
        }
      }
    }

    stderr: SplitParser {
      // Inherited by omarchy-shell, so this lands in `journalctl --user`.
      onRead: function (line) {
        if (line)
          console.log("omarchy-mcp:", line)
      }
    }

    onExited: function (exitCode, exitStatus) {
      root.serving = false
      if (!root.wantRunning) {
        root.phase = "stopped"
        return
      }
      root.failures += 1
      root.phase = "failed"
      root.lastError = "daemon exited with code " + exitCode
      console.warn("omarchy-mcp: daemon exited", exitCode, "- restarting in", root.backoffMs, "ms")

      // Nowhere else reports this. The only client is a language model, and it
      // will report "the tool is unreachable" long after the fact, if at all.
      // Warn once the restarts have clearly stopped being transient.
      if (root.failures === 3)
        notify.running = true

      respawn.interval = root.backoffMs
      respawn.restart()
    }
  }

  Timer {
    id: respawn
    repeat: false
    onTriggered: if (root.wantRunning) daemon.running = true
  }

  Timer {
    id: restartTimer
    interval: 250
    repeat: false
    onTriggered: root.start()
  }

  // Liveness, not just aliveness. A wedged HTTP loop still has a live pid, so
  // the bar widget would lie without an actual probe.
  //
  // The verdict is reached in onExited rather than onStreamFinished: both fire,
  // and deciding in one while the other also writes `serving` is a race. The
  // collector needs waitForEnd, or its text is empty when we read it.
  Process {
    id: health
    command: ["curl", "-fsS", "--max-time", "2", "http://127.0.0.1:" + root.port + "/health"]

    stdout: StdioCollector {
      id: healthOut
      waitForEnd: true
    }

    onExited: function (exitCode) {
      if (exitCode !== 0) {
        root.serving = false
        return
      }
      try {
        const body = JSON.parse(healthOut.text)
        root.serving = body.ok === true
        root.calls = Number(body.calls || 0)
        root.lastTool = String(body.last_tool || "")
        if (root.serving)
          root.failures = 0
      } catch (e) {
        root.serving = false
      }
    }
  }

  // Fast while it looks down, slow once it is answering: a server that is up
  // needs no attention, and one that is down is the thing worth noticing.
  Timer {
    interval: root.serving ? 10000 : 2000
    running: daemon.running
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!health.running) health.running = true
  }

  FileView {
    id: stateFile
    path: root.statePath
    printErrors: false
  }

  // Reachable from a terminal, and the way an agent or a script discovers what
  // this plugin can do:
  //   omarchy-shell io.github.bruce-forte.mcp-server status
  //   qs ipc -n -p "$OMARCHY_PATH/shell" show
  IpcHandler {
    target: root.pluginId

    function status(): string {
      return JSON.stringify({
        phase: root.phase,
        serving: root.serving,
        port: root.port,
        url: "http://127.0.0.1:" + root.port + "/mcp",
        pid: daemon.processId || 0,
        calls: root.calls,
        lastTool: root.lastTool,
        failures: root.failures,
        error: root.lastError
      }, null, 2)
    }

    function clientConfig(): string {
      clientConfigProc.running = true
      return "Printing the client setup command to the journal; run this to see it here:\n" +
             "  " + root.pluginDir + "bin/omarchy-mcpd --print-client-config"
    }

    function start(): string {
      root.failures = 0
      root.start()
      return "starting"
    }

    function stop(): string {
      root.stop()
      return "stopped"
    }

    function restart(): string {
      root.failures = 0
      root.restart()
      return "restarting"
    }

    function reloadConfig(): string {
      // The daemon reads its configuration at startup, so a reload is a restart.
      root.restart()
      return "reloading configuration (restarting the daemon)"
    }

    function rebuild(): string {
      rebuildProc.running = true
      return "rebuilding the environment; the daemon will restart when it finishes"
    }
  }

  Process {
    id: notify
    command: ["omarchy", "notification", "send", "-u", "critical",
      "MCP server", "The Omarchy MCP server keeps failing to start. "
      + "See journalctl --user -f, or run: omarchy-shell io.github.bruce-forte.mcp-server rebuild"]
  }

  Process {
    id: clientConfigProc
    command: [root.pluginDir + "bin/omarchy-mcpd", "--print-client-config"]
    stdout: StdioCollector {
      onStreamFinished: console.log("omarchy-mcp: client config:", this.text)
    }
  }

  Process {
    id: rebuildProc
    command: ["rm", "-rf",
      (Quickshell.env("XDG_STATE_HOME") || (Quickshell.env("HOME") + "/.local/state"))
        + "/" + root.pluginId + "/venv"]
    onExited: root.restart()
  }
}
