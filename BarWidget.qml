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
  property bool   filePermissionsOk: true

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
  readonly property bool permissionsOk: service ? service.permissionsOk : filePermissionsOk

  // The question currently on screen, if any. No file fallback: answering needs
  // the one-time token, which lives only on the service object and deliberately
  // never reaches the state file. A widget that has not resolved the service
  // yet cannot answer, and should not pretend it can.
  readonly property bool   asking: service ? service.asking : false
  readonly property string askRoute: service ? service.pendingRoute : ""
  readonly property var    askArgs: service ? service.pendingArgs : []
  readonly property string askTarget: service ? service.pendingTarget : ""

  // The delta. Same reasoning as the pending question: no file fallback,
  // because acknowledging needs a token that lives only on the service object.
  readonly property bool   needsReview: service ? service.needsReview : false
  readonly property string reviewHeadline: service ? service.reviewHeadline : ""
  readonly property var    review: service ? service.review : ({})

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
    function onPermissionsChecked(ok, detail) {
      root.permissionsCheckOk = ok
      root.permissionsCheck = detail
    }
  }

  property bool copied: false

  // The last permissions check, and whether it passed. Held rather than read
  // off the service every frame: it is a one-shot answer to a button press.
  property string permissionsCheck: ""
  property bool   permissionsCheckOk: true

  readonly property bool reviewLoadingOrEmpty: service
    ? (service.reviewLoading || !(service.review && service.review.arrivals))
    : false

  Timer {
    id: copiedFor
    interval: 1600
    repeat: false
    onTriggered: root.copied = false
  }

  // The log is read when the panel opens, not continuously: it is a file that
  // grows to a megabyte, and FileView has no seek.
  onOpenedChanged: {
    if (opened && service) {
      service.refreshRecent()
      // Only when there is one. The counts arrived on a frame; this reads the
      // rows, so a panel opened with nothing to review spawns nothing.
      service.refreshReview()
    }
  }

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
      filePermissionsOk = data.permissionsOk !== false
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
      if (record.event === "started") {
        // A session with no recorded end. The daemon cannot always write its
        // own `stopped` -- on `omarchy restart shell` the whole shell is torn
        // down and its children are reaped within milliseconds -- so a run of
        // bare "daemon started" rows reads as a bug when it is not one.
        // Derived when the log is read, never written: see ROADMAP.md F30.
        return record.unclosed ? "· daemon started · previous session not closed"
                               : "· daemon started"
      }
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
    if (record.event) {
      if (record.event === "dropped")
        return Color.urgent
      // Legible rather than alarming: an unclosed session is expected after a
      // shell restart, and is only worth distinguishing from a clean one.
      if (record.unclosed)
        return Qt.darker(Color.foreground, 1.4)
      return Qt.darker(Color.foreground, 1.8)
    }
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

        // First, above everything: a call is parked waiting for this. The
        // notification is the other surface, and it can only say yes -- these
        // are the two answers it has no room for.
        Column {
          width: parent.width
          spacing: Style.spacing.sm
          visible: root.asking

          PanelSectionHeader { text: "APPROVAL NEEDED" }

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            text: root.askRoute + (root.askArgs.length > 0
                    ? " " + root.askArgs.map(function (a) { return "'" + a + "'" }).join(" ")
                    : "")
            color: Color.urgent
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            font.bold: true
          }

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            // Named target or not, this line is always shown: "not resolvable"
            // is information, and a prompt that silently omits it reads as one
            // that checked.
            text: root.askTarget !== "" ? "Target: " + root.askTarget
                                        : "Target: not resolvable to a known object"
            color: Qt.darker(Color.foreground, 1.4)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }

          Row {
            spacing: Style.spacing.controlGap

            Button {
              text: "Allow once"
              bordered: true
              onClicked: root.service.answer("approve")
            }

            Button {
              text: "Always"
              bordered: true
              onClicked: root.service.answer("always")
            }

            Button {
              text: "Deny"
              bordered: true
              onClicked: root.service.answer("deny")
            }
          }

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "Always writes an allow rule for this exact command to "
                + "permissions.local.json. Ignoring this refuses it."
            color: Qt.darker(Color.foreground, 1.6)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }

          PanelSeparator { width: parent.width }
        }

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
        // a stray keystroke must not switch every disabled tool back on. Which
        // makes this the only place the person finds out.
        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          visible: !root.configOk
          text: "config.toml does not parse. Still running the previous configuration."
          color: Color.urgent
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        // Its own line, not folded into the one above. The two files fail
        // independently and are fixed in different places, and this one is the
        // more serious: at startup it stops the daemon rather than being
        // ignored, because running under rules nobody wrote is worse than not
        // running.
        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          visible: !root.permissionsOk
          text: root.serving
            ? "permissions.json does not load. Still running the permissions it had."
            : "permissions.json does not load, so the server did not start. Fix it, "
              + "press Check, then Start."
          color: Color.urgent
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        // The verdict of the last Check. With the daemon down this is the only
        // way to find out whether an edit worked without restarting to see.
        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          visible: root.permissionsCheck !== ""
          text: root.permissionsCheck
          color: root.permissionsCheckOk ? Qt.darker(Color.foreground, 1.4) : Color.urgent
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        // The delta: what an `omarchy update` changed under rules that did not
        // change. Above RECENT, because it is the one thing here that is
        // waiting on a person.
        Column {
          width: parent.width
          spacing: Style.spacing.sm
          visible: root.needsReview

          PanelSeparator { width: parent.width }

          Row {
            width: parent.width
            spacing: Style.spacing.controlGap

            PanelSectionHeader { text: "PERMISSIONS" }

            Text {
              text: root.reviewHeadline
              color: Color.urgent
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
          }

          // A rule that covers more than it did, without anybody editing it.
          // The most valuable row here and the one that reads as a warning.
          Repeater {
            model: root.review.widened || []

            Column {
              width: column.width
              spacing: 2

              Text {
                width: parent.width
                wrapMode: Text.WordWrap
                text: "Your " + modelData.effect + " rule '" + modelData.matcher
                    + "' now also covers " + modelData.routes.length + " command"
                    + (modelData.routes.length === 1 ? "" : "s")
                    + " you have not seen."
                color: Color.urgent
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                width: parent.width
                wrapMode: Text.WordWrap
                text: modelData.routes.join(", ")
                color: Qt.darker(Color.foreground, 1.4)
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }
            }
          }

          // A rule that has stopped matching. Silent loss of protection when it
          // is a deny: upstream renamed a route out from under it.
          Repeater {
            model: root.review.dead || []

            Text {
              width: column.width
              wrapMode: Text.WordWrap
              text: "Your " + modelData.effect + " rule '" + modelData.matcher
                  + "' no longer matches anything."
              color: Color.urgent
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
          }

          Repeater {
            model: root.review.arrivals || []

            Row {
              width: column.width
              spacing: Style.spacing.controlGap
              visible: modelData.quarantined || modelData.unclassified

              Text {
                width: parent.width - arrivalNote.width - parent.spacing
                elide: Text.ElideRight
                text: modelData.route
                color: Color.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                id: arrivalNote
                text: modelData.unclassified ? "new group · runs" : "held · asks once"
                color: modelData.unclassified ? Color.urgent
                                              : Qt.darker(Color.foreground, 1.4)
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }
            }
          }

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            visible: root.reviewLoadingOrEmpty
            text: "Reading the review…"
            color: Qt.darker(Color.foreground, 1.4)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }

          Row {
            spacing: Style.spacing.controlGap

            Button {
              text: "Acknowledge"
              bordered: true
              enabled: root.service !== null
              onClicked: root.service.acknowledge()
            }
          }

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "Acknowledging records what Omarchy ships now, so you are only "
                + "told about the next change. Held commands stop being asked about."
            color: Qt.darker(Color.foreground, 1.6)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
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
          // Validating without restarting matters here in a way it does not for
          // config.toml: a defective permissions document stops the daemon, so
          // without this the only way to test a fix is to try to start and see.
          Button {
            text: "Check permissions"
            bordered: true
            enabled: root.service !== null
            onClicked: root.service.checkPermissions()
          }

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
