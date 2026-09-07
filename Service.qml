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
// The daemon prints one JSON line to stdout per state change, and per tool
// call, and logs to stderr.
//
// State is also mirrored into a file, but no longer because it has to be. A
// bar widget cannot be reached by `shell call` -- that routes to panels and
// overlays only -- but it can reach *this object*, through
// `bar.shell.serviceFor(pluginId)`, which has no first-party restriction. The
// file remains because the widget is constructed before this service exists,
// so `serviceFor` returns null on every startup for as long as it takes the
// service host to catch up. Verified on a live desktop; see ROADMAP.md N6.
Item {
  id: root

  property var shell: null

  readonly property string pluginId: "io.github.bruce-forte.mcp-server"
  readonly property string pluginDir: Qt.resolvedUrl(".").toString().replace("file://", "")
  readonly property string statePath: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/omarchy-mcp.state"
  readonly property string stateDir: (Quickshell.env("XDG_STATE_HOME")
    || (Quickshell.env("HOME") + "/.local/state")) + "/" + root.pluginId

  // What the daemon last told us, and what we last observed ourselves.
  property string phase: "starting"      // starting | building | listening | failed | stopped
  property int    port: 8765
  property string lastError: ""
  property bool   serving: false          // proven by /health, not assumed from a pid
  property int    calls: 0                // tool calls served, from /health
  property string lastTool: ""

  // How many curated tools the daemon is offering, of how many it has. The
  // answer to "did my edit to config.toml take?", which nothing else on the
  // desktop could answer. Arrives on a `reloaded` frame at the moment of the
  // edit, and again on every /health poll so a missed frame heals.
  property int    tools: 0
  property int    toolsDeclared: 0
  // False when the daemon read config.toml and could not parse it. The daemon
  // keeps running the configuration it had; the bar is where that is visible.
  property bool   configOk: true

  // False when the permissions document would not load. Two flags rather than
  // one: the two files fail independently and are fixed in different places, so
  // a single lamp would say "something is wrong" and leave the person to guess.
  //
  // At startup this is fatal -- the daemon refuses to start rather than run
  // under rules nobody wrote -- and `lastError` carries the reason. At reload
  // the daemon keeps the document it had and only this goes false.
  property bool   permissionsOk: true

  property bool   wantRunning: true
  property int    failures: 0

  // The last few calls, read from the activity log when a panel asks for them.
  // Not kept live: the log is the thing that survives a crash, and merging it
  // with the frames below would mean deduplicating two accounts of one call.
  property var    recent: []
  property bool   recentLoading: false
  property bool   activityLogged: true     // false when log.activity is off

  // An agent just did something. The bar learns it here rather than from the
  // /health poll, which is up to ten seconds behind -- by which time the
  // desktop has already changed in front of the user.
  signal callSeen(string tool, string outcome)

  // The setup line reached the clipboard. The panel says so for a moment.
  signal clientConfigCopied()

  // A permissions check finished. The panel shows the verdict, which is the
  // whole point: with the daemon down it is the only surface left.
  signal permissionsChecked(bool ok, string detail)
  property string permissionsCheck: ""

  // The question currently on screen, if any. The panel is the second surface
  // it appears on: the notification carries one action (F25), so it can say yes
  // and nothing else, while this can also say "yes, always" and "no".
  //
  // `pendingToken` is a live capability for the length of one question. It is
  // held here and nowhere else -- never in `writeState()`, which lands in a file
  // under $XDG_RUNTIME_DIR, and never rendered, because a bar popup is on
  // screen, in screenshots, and in screen shares.
  property string pendingToken: ""
  property string pendingRoute: ""
  property var    pendingArgs: []
  property string pendingTarget: ""
  property string pendingMarker: ""
  readonly property bool asking: pendingToken !== ""

  // What has changed under the rules since anybody last acknowledged. The
  // counts and the token arrive on a frame; the rows are read on demand, so a
  // panel nobody opens costs nothing.
  //
  // `reviewToken` is held here and never written to the state file, for the same
  // reason a consent token is not: acknowledging is silent by design -- it
  // consumes a warning and raises nothing -- so the ability to do it must not
  // sit in a file an agent might reach.
  property string reviewToken: ""
  property string reviewHeadline: ""
  property int    reviewArrivals: 0
  property int    reviewWidened: 0
  property int    reviewDead: 0
  property var    review: ({})
  property bool   reviewLoading: false
  readonly property bool needsReview: reviewToken !== ""

  function refreshReview() {
    if (reviewProc.running || !root.needsReview)
      return
    reviewLoading = true
    reviewProc.running = true
  }

  function acknowledge() {
    if (reviewToken === "")
      return false
    ackProc.token = reviewToken
    clearReview()
    ackProc.running = true
    return true
  }

  function clearReview() {
    reviewToken = ""
    reviewHeadline = ""
    reviewArrivals = 0
    reviewWidened = 0
    reviewDead = 0
    review = ({})
  }

  // Answering goes through the same helper the notification's --exec runs, not
  // through a FileView here. That helper validates the token before using it as
  // a filename and is the one place the vocabulary is defined; a second writer
  // would be a second thing to get right, in a language with no tests in this
  // repository.
  function answer(verb) {
    if (pendingToken === "")
      return false
    answerProc.verb = verb
    answerProc.token = pendingToken
    // Cleared optimistically: the daemon confirms with an `answered` frame, but
    // the button should stop offering an answer the moment it is pressed.
    clearPending()
    answerProc.running = true
    return true
  }

  function clearPending() {
    pendingToken = ""
    pendingRoute = ""
    pendingArgs = []
    pendingTarget = ""
    pendingMarker = ""
  }

  // Whether a Stop survives a shell restart. Held in the state directory
  // rather than in memory: an off switch that turns itself back on at the next
  // login is not an off switch.
  property bool   autostartDecided: false

  // A restart is "start once the old one is actually gone", not "start in 250ms
  // and hope". The daemon can take seconds to exit -- it stops accepting, then
  // gives in-flight work its grace period -- and a timer that fired first found
  // the process still running, returned from start() without setting
  // wantRunning, and left the daemon stopped for good when it finally exited.
  property bool   restartPending: false

  // The daemon exits with this when its permissions document will not load.
  // Retrying cannot help -- the file has to be edited -- and the generic
  // "keeps failing, try rebuild" path would delete a perfectly good virtualenv
  // while the real cause sits in a JSON file. So this one exit code stops the
  // supervisor rather than starting the backoff.
  readonly property int exitBadPermissions: 78

  // A daemon that dies immediately -- a broken venv, a syntax error -- must not
  // be respawned in a tight loop. Back off, and cap the delay so a transient
  // failure still recovers without a shell restart.
  readonly property int backoffMs: Math.min(1000 * Math.pow(2, Math.min(failures, 6)), 60000)

  // Public start and stop carry intent, and intent is what persists. A restart
  // is not a stop, so it goes through the private pair and leaves the marker
  // alone -- otherwise a crash mid-restart would leave the daemon switched off
  // and nothing would say why.
  function start() {
    setAutostart(true)
    beginRunning()
  }

  function stop() {
    setAutostart(false)
    halt()
  }

  function beginRunning() {
    if (daemon.running)
      return
    wantRunning = true
    phase = "starting"
    daemon.running = true
  }

  function halt() {
    wantRunning = false
    if (daemon.running) {
      daemon.running = false   // SIGTERM
      sigkill.restart()        // ...and a deadline on it
    }
    phase = "stopped"
    serving = false
    writeState()
  }

  function restart() {
    restartPending = true
    const wasRunning = daemon.running
    halt()
    // Nothing will exit if it was already down, so nothing would start it.
    if (!wasRunning) {
      restartPending = false
      beginRunning()
    }
  }

  // Empty means "start me"; anything else means a person switched this off.
  // Written rather than deleted because FileView can write a file and cannot
  // remove one, and a marker whose contents say which is which needs no rm.
  function setAutostart(on) {
    autostartDecided = true
    stopMarker.setText(on ? "" : "stopped\n")
  }

  function decideAutostart(stopped) {
    if (autostartDecided)
      return
    autostartDecided = true
    if (stopped) {
      phase = "stopped"
      serving = false
      writeState()
      console.log("omarchy-mcp: not starting; stopped by the user. Start it from the"
        + " bar panel, or with: omarchy-shell " + root.pluginId + " start")
      return
    }
    beginRunning()
  }

  // Read the activity log for a panel that just opened. Spawned here rather
  // than in the widget because a bar surface exists per monitor and this is
  // one file: three screens would otherwise mean three readers of it.
  function refreshRecent() {
    if (tail.running)
      return
    recentLoading = true
    tail.running = true
  }

  // The setup line carries the bearer token, so it goes to the clipboard the
  // user is about to paste from and never to the screen.
  function copyClientConfig() {
    copyProc.running = true
  }

  // The daemon re-reads config.toml by itself within a couple of seconds. This
  // is the impatient path: SIGHUP makes it look now. Not a restart -- a restart
  // drops every attached MCP session, which is the thing N7 exists to avoid.
  function checkPermissions() {
    if (checkProc.running)
      return
    checkProc.running = true
  }

  function reloadConfig() {
    if (!daemon.running)
      return false
    daemon.signal(1)   // SIGHUP
    return true
  }

  function writeState() {
    stateFile.setText(JSON.stringify({
      phase: root.phase,
      serving: root.serving,
      port: root.port,
      calls: root.calls,
      lastTool: root.lastTool,
      tools: root.tools,
      toolsDeclared: root.toolsDeclared,
      configOk: root.configOk,
      permissionsOk: root.permissionsOk,
      error: root.lastError,
      pid: daemon.processId || 0
    }))
  }

  onPhaseChanged: writeState()
  onServingChanged: writeState()

  // Not `start()`: whether to start at all is a question the state directory
  // answers, and the answer arrives asynchronously.
  FileView {
    id: stopMarker
    path: root.stateDir + "/autostart-off"
    printErrors: false
    onLoaded: root.decideAutostart(text().trim() !== "")
    onLoadFailed: root.decideAutostart(false)
  }

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

          // A call is not a lifecycle state: the daemon is still listening.
          // Counted here so the bar reflects it now; the next /health poll
          // carries the daemon's own count and corrects any drift.
          if (frame.state === "call") {
            root.calls += 1
            root.lastTool = frame.tool || ""
            root.callSeen(frame.tool || "", frame.outcome || "")
            root.writeState()
            return
          }

          // A question is on screen. Not a lifecycle state: the daemon is
          // still listening, and is parked on one call.
          if (frame.state === "asking") {
            root.pendingToken = frame.token || ""
            root.pendingRoute = frame.route || ""
            root.pendingArgs = frame.args || []
            root.pendingTarget = frame.target || ""
            root.pendingMarker = frame.marker || ""
            return
          }

          // It closed, however it closed. A bar surface exists once per screen,
          // so this is what takes a spent question off the panels that did not
          // answer it.
          if (frame.state === "answered") {
            if (frame.marker === root.pendingMarker || root.pendingMarker === "")
              root.clearPending()
            return
          }

          // Something changed under the rules and nobody has looked at it.
          if (frame.state === "review") {
            root.reviewToken = frame.token || ""
            root.reviewHeadline = frame.headline || ""
            root.reviewArrivals = Number(frame.arrivals || 0)
            root.reviewWidened = Number(frame.widened || 0)
            root.reviewDead = Number(frame.dead || 0)
            return
          }

          // Not a lifecycle state either: the daemon re-read its config and
          // is still whatever it was before.
          if (frame.state === "reloaded") {
            root.tools = Number(frame.tools || 0)
            root.toolsDeclared = Number(frame.declared || 0)
            root.configOk = frame.config_ok !== false
            root.permissionsOk = frame.permissions_ok !== false
            return
          }

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
      sigkill.stop()
      root.serving = false

      if (root.restartPending) {
        root.restartPending = false
        root.beginRunning()
        return
      }

      if (!root.wantRunning) {
        root.phase = "stopped"
        return
      }

      if (exitCode === root.exitBadPermissions) {
        // The daemon has already said what is wrong -- on stderr, in a `failed`
        // frame, and in a critical notification naming the file. Nothing to add
        // and nothing to retry.
        root.wantRunning = false
        root.phase = "failed"
        root.permissionsOk = false
        if (root.lastError === "")
          root.lastError = "permissions.json does not load; the server did not start"
        root.writeState()
        console.warn("omarchy-mcp: permissions rejected; not restarting until it is fixed")
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

  // SIGTERM is asked politely and is not always answered. The daemon bounds its
  // own graceful shutdown, so reaching this means it is wedged rather than
  // busy -- and a supervisor that cannot end what it started is not supervising.
  // Longer than the daemon's own grace period, so the ordinary path is its
  // clean exit and this only ever fires on a real fault.
  Timer {
    id: sigkill
    interval: 5000
    repeat: false
    onTriggered: {
      if (!daemon.running)
        return
      console.warn("omarchy-mcp: daemon ignored SIGTERM for 5s; killing it")
      daemon.signal(9)
    }
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
        root.tools = Number(body.tools || 0)
        root.toolsDeclared = Number(body.tools_declared || 0)
        if (root.serving)
          root.failures = 0
      } catch (e) {
        root.serving = false
      }
      // Written once, after every field is set. Publishing from a property
      // change handler instead means the first field assigned writes a snapshot
      // that does not yet contain the others.
      root.writeState()
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
        tools: root.tools,
        toolsDeclared: root.toolsDeclared,
        configOk: root.configOk,
        permissionsOk: root.permissionsOk,
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
      return "stopped; it will stay stopped across a restart until you start it again"
    }

    function recent(): string {
      // The panel's list, for a terminal. Reads the log directly, so it
      // answers whether or not the daemon is running.
      root.refreshRecent()
      return "reading the activity log; see the bar panel, or run:\n"
           + "  " + root.pluginDir + "bin/omarchy-mcpd --tail 20"
    }

    function copyClientConfig(): string {
      root.copyClientConfig()
      return "the client setup command is on the clipboard"
    }

    function restart(): string {
      root.failures = 0
      root.restart()
      return "restarting"
    }

    function checkPermissions(): string {
      // The daemon refuses to start on a defective permissions document, so
      // there has to be a way to test a fix without restarting to find out.
      checkProc.running = true
      return "checking ~/.config/omarchy/mcp/permissions.json; see the journal, or run:\n"
           + "  " + root.pluginDir + "bin/omarchy-mcpd --check-permissions"
    }

    function review(): string {
      // Read-only. Acknowledging is the panel's and the helper's: an agent can
      // reach this target (N12), and acknowledging is silent, so a verb that
      // did it here would let an agent clear its own review with nothing on
      // screen.
      if (!root.needsReview)
        return "nothing to review"
      return root.reviewHeadline + "\nSee the bar panel, or run:\n"
           + "  " + root.pluginDir + "bin/omarchy-mcpd --review"
    }

    function pending(): string {
      // Read-only on purpose. An agent can reach this plugin's own target
      // (N12), so a verb that *answered* a question would let it approve its
      // own call. Answering is the panel's, and the notification's.
      if (!root.asking)
        return "nothing is waiting for an answer"
      return "waiting for an answer: " + root.pendingRoute
           + (root.pendingTarget !== "" ? " — " + root.pendingTarget : "")
           + "\nAnswer it in the bar panel, or click the notification."
    }

    function reloadConfig(): string {
      // No longer a restart: the daemon re-reads the file in place and tells
      // attached clients if the tool set moved. It would do this within two
      // seconds anyway; SIGHUP is only the impatient path.
      if (!root.reloadConfig())
        return "the daemon is not running; start it first"
      return "re-reading ~/.config/omarchy/mcp/config.toml now"
    }

    function rebuild(): string {
      rebuildProc.running = true
      return "rebuilding the environment; the daemon will restart when it finishes"
    }
  }

  // Validates the permissions document without starting anything. Read-only,
  // and the one verb that is useful precisely when the daemon is down.
  Process {
    id: checkProc
    command: [root.pluginDir + "bin/omarchy-mcpd", "--check-permissions"]

    stdout: StdioCollector {
      id: checkOut
      waitForEnd: true
    }

    onExited: function (exitCode) {
      root.permissionsCheck = checkOut.text.trim()
      root.permissionsChecked(exitCode === 0, root.permissionsCheck)
      console.log("omarchy-mcp: permissions check exited", exitCode, "\n" + checkOut.text)
    }
  }

  // Writes the answer through the helper. argv rather than stdin because the
  // token is not a secret from the user's own processes -- it is a secret from
  // the *model*, which never sees stdout or this argv.
  Process {
    id: answerProc
    property string verb: ""
    property string token: ""
    command: [root.pluginDir + "bin/omarchy-mcp-consent", answerProc.verb, answerProc.token]

    onExited: function (exitCode) {
      if (exitCode !== 0)
        console.warn("omarchy-mcp: could not record the answer; helper exited", exitCode)
      answerProc.token = ""
    }
  }

  // The review's rows, read when a panel asks. Computed by the CLI from the
  // same inputs the daemon used, so it answers whether or not one is running.
  Process {
    id: reviewProc
    command: [root.pluginDir + "bin/omarchy-mcpd", "--review", "--json"]

    stdout: StdioCollector {
      id: reviewOut
      waitForEnd: true
    }

    onExited: function (exitCode) {
      root.reviewLoading = false
      if (exitCode !== 0) {
        console.warn("omarchy-mcp: could not read the review; exited", exitCode)
        return
      }
      try {
        root.review = JSON.parse(reviewOut.text)
      } catch (e) {
        console.warn("omarchy-mcp: could not parse the review:", e)
      }
    }
  }

  Process {
    id: ackProc
    property string token: ""
    command: [root.pluginDir + "bin/omarchy-mcp-consent", "acknowledge", ackProc.token]

    onExited: function (exitCode) {
      if (exitCode !== 0)
        console.warn("omarchy-mcp: could not acknowledge; helper exited", exitCode)
      ackProc.token = ""
    }
  }

  Process {
    id: notify
    command: ["omarchy", "notification", "send", "-u", "critical",
      "MCP server", "The Omarchy MCP server keeps failing to start. "
      + "See journalctl --user -f, or run: omarchy-shell io.github.bruce-forte.mcp-server rebuild"]
  }

  // Reads the log for a panel. `--tail --json` answers with an envelope rather
  // than a bare array: an empty list alone cannot say whether nothing has
  // happened or the log is switched off, and the panel has to tell a user
  // which one they are looking at.
  Process {
    id: tail
    command: [root.pluginDir + "bin/omarchy-mcpd", "--tail", "8", "--json"]

    stdout: StdioCollector {
      id: tailOut
      waitForEnd: true
    }

    onExited: function (exitCode) {
      root.recentLoading = false
      if (exitCode !== 0) {
        root.recent = []
        console.warn("omarchy-mcp: could not read the activity log; --tail exited", exitCode)
        return
      }
      try {
        const body = JSON.parse(tailOut.text)
        root.activityLogged = body.activity !== false
        root.recent = body.records || []
      } catch (e) {
        root.recent = []
        console.warn("omarchy-mcp: could not parse the activity log:", e)
      }
    }
  }

  // The setup line carries the bearer token, so it goes to the clipboard the
  // user is about to paste from and never to the screen: a bar popup is on
  // screen, in screenshots, and in screen shares.
  //
  // Two processes and a pipe rather than `wl-copy <text>`, because argv is
  // world-readable through /proc. The same reason `panels/network/Panel.qml`
  // sends a wifi password over stdin.
  Process {
    id: copyProc
    command: [root.pluginDir + "bin/omarchy-mcpd", "--print-client-config"]

    stdout: StdioCollector {
      id: copyOut
      waitForEnd: true
    }

    onExited: function (exitCode) {
      if (exitCode !== 0) {
        console.warn("omarchy-mcp: could not read the client config; exited", exitCode)
        return
      }
      clipboard.payload = copyOut.text
      clipboard.running = true
    }
  }

  Process {
    id: clipboard
    property string payload: ""
    command: ["wl-copy"]
    stdinEnabled: true

    onStarted: {
      write(payload)
      payload = ""
      // wl-copy reads to EOF, so the write has to be finished, not just sent.
      stdinEnabled = false
    }

    onExited: function (exitCode) {
      if (exitCode === 0)
        root.clientConfigCopied()
      else
        console.warn("omarchy-mcp: could not reach the clipboard; wl-copy exited", exitCode)
    }
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
