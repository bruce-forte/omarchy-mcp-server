import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

// Says whether the MCP server is actually serving, shows what an agent has
// been doing, and lets the user switch it off.
//
// This exists because the only other client of this daemon is a language
// model. Nothing else would ever notice that the server stopped: no window
// closes, no command fails in front of you, and by the time an agent reports
// "I could not reach the tool" you are already debugging the wrong thing. The
// same argument applies in the other direction -- nothing on the desktop marks
// the moment an agent changes something -- which is what the panel and the
// pulse are for.
//
// The distinction the icon draws is serving versus running. A wedged HTTP loop
// still has a live process, so the service probes /health and this reads the
// answer.
//
// Rooted in `Ui/Panel` rather than `Ui/BarWidget`: a bar-widget entry point is
// allowed to be a panel, which is how omarchy.power, .network and .agents are
// built. `Bar.findPanelWidget` recognises it by open/close/opened, which Panel
// provides, and `shell summon/hide/toggle` then reach it directly.
Panel {
  id: root

  moduleName: "io.github.bruce-forte.mcp-server"

  // Deliberately empty, so Panel's own IpcHandler stays disabled. Service.qml
  // already owns this plugin's IPC target, and a target only ever routes to
  // one handler while a bar surface exists per monitor -- which is exactly why
  // the shell routes summon/toggle to the live widget instead.
  ipcTarget: ""

  // The live service object, or null. `serviceFor` has no first-party
  // restriction, so a plugin's widget can reach its own service directly.
  //
  // This must stay a binding. The bar widget is constructed before the service
  // exists, so the first evaluation is null on every single startup, and it
  // only becomes the service because the binding re-runs when the shell's
  // service map changes. The imperative version of this line latches null
  // forever. Measured on a live desktop; see ROADMAP.md N6.
  readonly property var service: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor(root.moduleName) : null

  // What the state file said, for the window before the service resolves and
  // for any shell that stops handing out service objects.
  property string filePhase: "starting"
  property bool   fileServing: false
  property int    filePort: 8765
  property int    fileCalls: 0
  property string fileLastTool: ""
  property string fileError: ""
  property int    fileTools: 0
  property int    fileToolsDeclared: 0
  property bool   fileConfigOk: true

  readonly property string phase: service ? service.phase : filePhase
  readonly property bool   serving: service ? service.serving : fileServing
  readonly property int    port: service ? service.port : filePort
  readonly property int    calls: service ? service.calls : fileCalls
  readonly property string lastTool: service ? service.lastTool : fileLastTool
  readonly property string lastError: service ? service.lastError : fileError

  // How many curated tools are on, of how many exist, and whether the last
  // read of config.toml was usable. This is the answer to "did my edit take?",
  // which nothing else on the desktop gives.
  readonly property int  tools: service ? service.tools : fileTools
  readonly property int  toolsDeclared: service ? service.toolsDeclared : fileToolsDeclared
  readonly property bool configOk: service ? service.configOk : fileConfigOk

  readonly property var recent: service ? service.recent : []
  readonly property bool recentLoading: service ? service.recentLoading : false
  readonly property bool activityLogged: service ? service.activityLogged : true

  // Nerd Font: a plug, connected or not.
  readonly property string icon: serving ? "󱘖" : "󰚦"

  readonly property string label: {
    if (serving)
      return icon
    if (phase === "building")
      return icon + " …"
    return icon + " !"
  }

  readonly property string tooltip: {
    if (serving) {
      var up = "MCP server on 127.0.0.1:" + port
      // The call count is the only visible trace an agent leaves. Without it
      // there is nothing on the desktop that says anything happened.
      up += "\n" + calls + (calls === 1 ? " tool call" : " tool calls") + " served"
      if (lastTool !== "")
        up += "\nLast: " + lastTool
      up += "\nClick for recent calls and controls"
      return up
    }
    if (phase === "building")
      return "MCP server: building its environment on first run"
    if (phase === "stopped")
      return "MCP server: stopped\nClick to start it"
    var text = "MCP server: not serving (" + phase + ")"
    if (lastError !== "")
      text += "\n" + lastError
    text += "\nSee `journalctl --user -f` for the reason"
    return text
  }

  // A call just happened. Held briefly so a burst of them reads as sustained
  // activity rather than a flicker.
  property bool active: false

  function noteCall() {
    root.active = true
    pulse.restart()
  }

  Timer {
    id: pulse
    interval: 700
    repeat: false
    onTriggered: root.active = false
  }

  Connections {
    // Null until the service resolves; Connections handles that itself.
    target: root.service
    function onCallSeen(tool, outcome) { root.noteCall() }
    function onClientConfigCopied() { root.copied = true; copiedFor.restart() }
  }

  property bool copied: false

  Timer {
    id: copiedFor
    interval: 1600
    repeat: false
    onTriggered: root.copied = false
  }

  // The log is read when the panel opens, not continuously: it is a file that
  // grows to a megabyte, and FileView has no seek.
  onOpenedChanged: if (opened && service) service.refreshRecent()

  function apply(raw) {
    if (!raw || raw.length === 0) {
      fileServing = false
      filePhase = "stopped"
      return
    }

    try {
      var data = JSON.parse(raw)
      filePhase = String(data.phase || "starting")
      fileServing = data.serving === true
      filePort = Number(data.port || 8765)
      fileCalls = Number(data.calls || 0)
      fileLastTool = String(data.lastTool || "")
      fileError = String(data.error || "")
      fileTools = Number(data.tools || 0)
      fileToolsDeclared = Number(data.toolsDeclared || 0)
      fileConfigOk = data.configOk !== false
    } catch (e) {
      // A partial read; the next write brings a whole one.
      fileServing = false
    }
  }

  // One row of the log, as a person reads it. Arguments are never among the
  // fields taken: a clipboard write's argument is the clipboard, and this is a
  // popup on a desktop. They stay in the 0600 file, for `--tail` in a terminal.
  //
  // Not every line is a call. The log also carries its own events, and they
  // are rendered rather than skipped -- `dropped` most of all. A log that
  // quietly leaves out the record of what it lost is worse than no log, which
  // is why the writer emits that event in the first place.
  function rowLabel(record) {
    if (record.event) {
      if (record.event === "dropped")
        return "· " + Number(record.n || 0) + " records dropped"
      if (record.event === "started")
        return "· daemon started"
      if (record.event === "stopped")
        return "· daemon stopped"
      if (record.event === "reloaded") {
        var moved = []
        if (record.added && record.added.length)
          moved.push("+" + record.added.length)
        if (record.removed && record.removed.length)
          moved.push("-" + record.removed.length)
        if (record.policy)
          moved.push("policy")
        return "· config reloaded" + (moved.length ? " (" + moved.join(" ") + ")" : "")
      }
      if (record.event === "config_rejected")
        return "· config not applied"
      return "· " + String(record.event)
    }

    var parts = [String(record.tool || "")]
    if (record.route)
      parts.push(String(record.route))
    if (record.target)
      parts.push("→ " + String(record.target))
    return parts.join("  ")
  }

  function rowDetail(record) {
    if (record.event)
      return ""
    var outcome = String(record.outcome || "")
    if (record.consent === "accepted")
      outcome += " · approved"
    if (record.ms !== undefined)
      outcome += "  " + Number(record.ms) + "ms"
    return outcome
  }

  function rowColor(record) {
    if (record.event)
      return record.event === "dropped" ? Color.urgent : Qt.darker(Color.foreground, 1.8)
    return Color.foreground
  }

  function detailColor(record) {
    var outcome = String(record.outcome || "")
    if (outcome === "ok")
      return Qt.darker(Color.foreground, 1.4)
    if (outcome === "refused")
      return Color.accent
    return Color.urgent
  }

  FileView {
    id: stateFile

    path: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/omarchy-mcp.state"
    watchChanges: true
    printErrors: false

    // text() is stale inside the change signal, so both paths go through
    // reload -> onLoaded and always parse fresh content.
    onLoaded: root.apply(text())
    onFileChanged: reload()
    onLoadFailed: root.apply("")
  }

  // A server that is up needs no attention; one that is down is the whole point
  // of the widget, so the default keeps it visible either way.
  visible: root.serving || root.setting("showWhenStopped", true)
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.label
    fontSize: Style.font.caption
    horizontalMargin: 6
    tooltipText: root.tooltip
    active: root.active
    onPressed: function (b) { root.toggle() }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keys
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(520))

    PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function (direction) { root.switchPanel(direction) }

      Column {
        id: column
        width: parent.width
        spacing: Style.spacing.md

        Text {
          text: root.serving ? "Serving on 127.0.0.1:" + root.port
                             : "Not serving · " + root.phase
          color: root.serving ? Color.foreground : Color.urgent
          font.family: Style.font.family
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }

        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          visible: text !== ""
          text: {
            if (root.serving)
              return root.calls + (root.calls === 1 ? " tool call" : " tool calls") + " served"
            if (root.phase === "stopped")
              return "Stopped. It will stay stopped until you start it again."
            return root.lastError !== "" ? root.lastError
                                         : "See `journalctl --user -f` for the reason"
          }
          color: Qt.darker(Color.foreground, 1.4)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        // Not folded into the line above: that one is about the daemon, this
        // one is about what an agent can reach, and they change for different
        // reasons. Hidden until the daemon has said, so a starting server does
        // not read as "0 tools".
        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          visible: root.toolsDeclared > 0
          text: {
            var line = root.tools + " of " + root.toolsDeclared + " tools offered"
            if (root.tools < root.toolsDeclared)
              line += " · " + (root.toolsDeclared - root.tools) + " switched off in config.toml"
            return line
          }
          color: Qt.darker(Color.foreground, 1.4)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        // The daemon keeps serving when config.toml stops parsing, on purpose:
        // a stray keystroke must not empty the policy or switch tools back on.
        // Which makes this the only place the person finds out.
        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          visible: !root.configOk
          text: "config.toml does not parse. Still running the previous configuration."
          color: Color.urgent
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        PanelSeparator { width: parent.width }

        PanelSectionHeader { text: "RECENT" }

        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          visible: root.recent.length === 0
          text: {
            if (root.recentLoading)
              return "Reading the log…"
            if (!root.activityLogged)
              return "The activity log is off. Set log.activity = true in "
                   + "~/.config/omarchy/mcp/config.toml to keep a record of what agents do."
            return "Nothing recorded yet."
          }
          color: Qt.darker(Color.foreground, 1.4)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        Repeater {
          model: root.recent

          Row {
            width: column.width
            spacing: Style.spacing.controlGap

            Text {
              width: parent.width - detail.width - parent.spacing
              elide: Text.ElideRight
              text: root.rowLabel(modelData)
              color: root.rowColor(modelData)
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }

            Text {
              id: detail
              text: root.rowDetail(modelData)
              color: root.detailColor(modelData)
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
          }
        }

        PanelSeparator { width: parent.width }

        Row {
          spacing: Style.spacing.controlGap

          Button {
            text: root.serving || root.phase !== "stopped" ? "Stop" : "Start"
            bordered: true
            enabled: root.service !== null
            onClicked: {
              if (root.serving || root.phase !== "stopped")
                root.service.stop()
              else
                root.service.start()
            }
          }

          Button {
            text: "Restart"
            bordered: true
            enabled: root.service !== null
            onClicked: root.service.restart()
          }

          // The daemon re-reads config.toml by itself within two seconds. This
          // is here for the case that makes waiting unbearable: the file was
          // rejected, you have just fixed it, and you want the answer now
          // rather than a wait you cannot tell from "still broken".
          Button {
            text: "Reload config"
            bordered: true
            enabled: root.service !== null && root.serving
            onClicked: root.service.reloadConfig()
          }

          Button {
            text: root.copied ? "Copied" : "Copy client config"
            bordered: true
            enabled: root.service !== null
            // The setup line carries the bearer token. It goes to the
            // clipboard and is never rendered here.
            onClicked: root.service.copyClientConfig()
          }
        }
      }
    }
  }
}
