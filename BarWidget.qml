import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons

// Says whether the MCP server is actually serving.
//
// This exists because the only other client of this daemon is a language model.
// Nothing else would ever notice that the server stopped: no window closes, no
// command fails in front of you, and by the time an agent reports "I could not
// reach the tool" you are already debugging the wrong thing.
//
// The distinction the icon draws is serving versus running. A wedged HTTP loop
// still has a live process, so the service plugin probes /health and writes the
// answer here; this widget only reads.
BarWidget {
  id: root

  moduleName: "io.github.bruce-forte.mcp-server"

  property string phase: "starting"
  property bool serving: false
  property int port: 8765
  property int calls: 0
  property string lastTool: ""
  property string lastError: ""

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
      up += "\nRun `omarchy-shell " + root.moduleName + " clientConfig` to connect a client"
      return up
    }
    if (phase === "building")
      return "MCP server: building its environment on first run"
    if (phase === "stopped")
      return "MCP server: stopped\nStart it with `omarchy-shell " + root.moduleName + " start`"
    var text = "MCP server: not serving (" + phase + ")"
    if (lastError !== "")
      text += "\n" + lastError
    text += "\nSee `journalctl --user -f` for the reason"
    return text
  }

  function apply(raw) {
    if (!raw || raw.length === 0) {
      serving = false
      phase = "stopped"
      return
    }

    try {
      var data = JSON.parse(raw)
      phase = String(data.phase || "starting")
      serving = data.serving === true
      port = Number(data.port || 8765)
      calls = Number(data.calls || 0)
      lastTool = String(data.lastTool || "")
      lastError = String(data.error || "")
    } catch (e) {
      // A partial read; the next write brings a whole one.
      serving = false
    }
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
  }
}
