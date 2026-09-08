import QtQuick
import QtQuick.Controls
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
  readonly property bool   askingSuppressed: service ? service.askingSuppressed : false
  readonly property var    coolingRoutes: service ? service.coolingRoutes : []
  readonly property bool   canPrune: service ? service.canPrune : false
  readonly property int    prunableRules: service ? service.prunableRules : 0

  // The rules themselves. No file fallback: this is read by spawning the CLI,
  // which is the service's job -- a bar surface exists once per screen, and
  // three monitors reading the registry three times is the thing the service
  // object exists to prevent.
  readonly property var  rules: service ? service.rules : ({})
  readonly property bool rulesLoading: service ? service.rulesLoading : false
  readonly property bool rulesRead: service ? service.rulesRead : false
  readonly property bool canRemove: service ? service.canRemove : false

  //: The rows of the two files, split, because each file is its own group with
  //: its own Edit button -- and only one of them has rules this panel may take
  //: away.
  readonly property var localRules: (rules.rules || []).filter(function (r) {
    return r.source === "permissions.local.json"
  })
  readonly property var ownRules: (rules.rules || []).filter(function (r) {
    return r.source !== "permissions.local.json"
  })
  readonly property int flaggedRules: (rules.rules || []).filter(function (r) {
    return r.error !== undefined || r.void !== undefined
        || r.shadowed !== undefined || r.redundant !== undefined
  }).length

  //: The strip, and which body is showing. Per widget instance, because a bar
  //: surface exists once per screen, and reset on open: the strip is navigated
  //: by feel, so a card that sometimes opens in the middle of its own strip
  //: makes every keystroke after it land somewhere unpredicted.
  readonly property var tabs: ["Summary", "Log", "Rules"]
  property string tab: "Summary"

  //: The strip's model. `ButtonGroup` takes {value, label} options, which is
  //: what lets the marker sit *on* the tab it is about -- a dot rendered after
  //: the strip would say that something needs attention without saying where.
  readonly property var tabOptions: [
    { "value": "Summary", "label": "Summary" },
    { "value": "Log", "label": "Log" },
    { "value": "Rules", "label": root.rulesNeedAttention ? "Rules ●" : "Rules" }
  ]

  //: What tabs take away is everything being visible at once. A dot, not a
  //: count: the question a marker answers is *is there anything over there*,
  //: and a number is ambiguous the moment two kinds of thing can be counted.
  //
  //  Whatever this covers has to be what the tab actually shows, or the marker
  //  is a promise that fails silently -- an absent dot reads as "nothing there".
  readonly property bool rulesNeedAttention: needsReview || canPrune || flaggedRules > 0

  // --- the focus ring ------------------------------------------------------
  //
  // Keyed rather than indexed, because half the stops do not exist until the
  // daemon says so: a Remove per grant, a Prune when something is dead, an
  // Acknowledge when a review is waiting. An index into a list that changes
  // under the cursor points at a different button than the one that was lit.
  //
  // Rows, not a flat list: `Up`/`Down` cross between rows and `Left`/`Right`
  // walk within one, which is how every keyboard-driven panel in this shell
  // behaves. A horizontal row -- the three answers, the daemon buttons -- is
  // one vertical stop.
  property int cursorRow: 0
  property int cursorCol: 0

  readonly property var focusRows: {
    var rows = []
    if (root.asking)
      rows.push(["ask:once", "ask:always", "ask:deny"])
    if (root.tab === "Summary")
      rows.push(["edit:config"])
    if (root.tab === "Rules") {
      rows.push(["edit:permissions"])
      rows.push(["edit:local"])
      if (root.canRemove) {
        var grants = root.localShown
        for (var i = 0; i < grants.length; i++) {
          if (grants[i].effect === "allow")
            rows.push(["remove:" + grants[i].matcher])
        }
      }
      if (root.canPrune)
        rows.push(["prune"])
      if (root.needsReview)
        rows.push(["acknowledge"])
    }
    rows.push(["action:power", "action:restart", "action:check",
               "action:reload", "action:copy"])
    return rows
  }

  readonly property string cursorKey: {
    var rows = root.focusRows
    if (root.cursorRow < 0 || root.cursorRow >= rows.length)
      return ""
    var row = rows[root.cursorRow]
    return root.cursorCol >= 0 && root.cursorCol < row.length ? row[root.cursorCol] : ""
  }

  //: The rows a Remove is actually drawn on. The ring has to agree with what
  //: is on screen, so the cap lives here rather than in the delegate.
  readonly property int localCap: 8
  readonly property var localShown: localRules.slice(0, localCap)

  //: Whether this tab's body contributes any stops. The Log contributes none,
  //: which is what frees `Up`/`Down` to scroll it -- a Log you cannot scroll
  //: from the keyboard is not a Log tab.
  readonly property bool bodyHasControls: tab !== "Log"

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
    if (opened)
      root.tab = "Summary"
    // On open, never on arrival. A question appearing under an already-open
    // panel must not move the cursor under somebody's fingers: that is how a
    // call gets approved by an Enter meant for something else.
    if (opened)
      root.restCursor()
    if (opened && service) {
      service.refreshRecent()
      // Only when there is one. The counts arrived on a frame; this reads the
      // rows, so a panel opened with nothing to review spawns nothing.
      service.refreshReview()
      // Every open: the files move under this panel -- a hand edit, an Always
      // click on another screen -- and the rows are cheap to recompute and
      // wrong to cache across an open.
      service.refreshRules()
    }
  }

  function bodyFlick() {
    if (tab === "Summary")
      return summaryFlick
    if (tab === "Log")
      return logFlick
    return rulesFlick
  }

  function scrollBody(dy) {
    var flick = bodyFlick()
    if (!flick)
      return
    flick.contentY = Math.max(0, Math.min(flick.contentY + dy * Style.space(56),
                                          Math.max(0, flick.contentHeight - flick.height)))
  }

  function moveCursor(dx, dy) {
    var rows = focusRows
    if (rows.length === 0)
      return
    if (dy !== 0) {
      // On a tab with nothing to focus in its body, the arrows are free and
      // the body is the thing that needs them.
      if (!bodyHasControls) {
        scrollBody(dy)
        return
      }
      cursorRow = Math.max(0, Math.min(cursorRow + dy, rows.length - 1))
      cursorCol = 0
      return
    }
    if (dx !== 0) {
      var row = rows[Math.max(0, Math.min(cursorRow, rows.length - 1))]
      cursorCol = Math.max(0, Math.min(cursorCol + dx, row.length - 1))
    }
  }

  // Scroll to whatever the cursor just landed on. Called by the button itself,
  // because a keyed cursor has no item to measure -- the item knows.
  function reveal(item) {
    var flick = bodyFlick()
    if (!item || !flick || flick.contentY === undefined)
      return
    var top = item.mapToItem(flick.contentItem, 0, 0).y
    var bottom = top + item.height
    var margin = Style.space(12)
    if (top - margin < flick.contentY)
      flick.contentY = Math.max(0, top - margin)
    else if (bottom + margin > flick.contentY + flick.height)
      flick.contentY = Math.min(Math.max(0, flick.contentHeight - flick.height),
                                bottom + margin - flick.height)
  }

  function activateCursor() {
    var key = cursorKey
    if (!service || key === "")
      return
    if (key === "ask:once")   { service.answer("approve"); return }
    if (key === "ask:always") { service.answer("always"); return }
    if (key === "ask:deny")   { service.answer("deny"); return }
    if (key === "edit:config")      { service.editFile("config"); return }
    if (key === "edit:permissions") { service.editFile("permissions"); return }
    if (key === "edit:local")       { service.editFile("local"); return }
    if (key.indexOf("remove:") === 0) {
      service.revoke("allow", key.substring("remove:".length))
      return
    }
    if (key === "prune")       { service.prune(); return }
    if (key === "acknowledge") { service.acknowledge(); return }
    if (key === "action:power") {
      if (root.serving || root.phase !== "stopped")
        service.stop()
      else
        service.start()
      return
    }
    if (key === "action:restart") { service.restart(); return }
    if (key === "action:check")   { service.checkPermissions(); return }
    if (key === "action:reload")  { service.reloadConfig(); return }
    if (key === "action:copy")    { service.copyClientConfig(); return }
  }

  // `[` and `]`, because Tab belongs to the bar. They wrap, and with three tabs
  // that puts every tab at most one keystroke away in one direction or the
  // other -- which is also why there are no 1/2/3 shortcuts.
  function stepTab(direction) {
    var i = tabs.indexOf(tab)
    if (i < 0)
      i = 0
    tab = tabs[(i + direction + tabs.length) % tabs.length]
  }

  //: The cursor lands somewhere sensible whenever the ring changes shape: on
  //: the answers when a question is up *at open*, and on the daemon buttons
  //: otherwise, since those are on every tab and are what a person opens this
  //: panel to press.
  function restCursor() {
    cursorRow = root.asking ? 0 : Math.max(0, focusRows.length - 1)
    cursorCol = 0
  }

  onTabChanged: restCursor()

  // Which finding a row carries, worst first. A rule has at most one: an error
  // stops the daemon, and saying it is also redundant would be two lines about
  // one broken sentence.
  //
  // The second element is a *line*, not the reason. `--permissions` prints the
  // sentence, which says what to do about it; a bar popup is fitted to a fixed
  // height, and five wrapped lines per rule walk the whole section out through
  // the bottom of the frame.
  function flagOf(row) {
    const levels = ["error", "void", "shadowed", "redundant"]
    for (let i = 0; i < levels.length; i++) {
      const level = levels[i]
      if (row[level] === undefined)
        continue
      if (level === "void")
        return [level, "matches nothing this Omarchy ships"]
      if (level === "error")
        return [level, "the daemon will not start with this"]
      return [level, "by " + row.by + " in " + row.bySource]
    }
    return ["", ""]
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

  // The panel is three bodies between two permanent bands.
  //
  // Tabs, because everything in one column meant nothing was prominent: a
  // parked question, the daemon's health, a cooldown, dead rules, a review and
  // the log were all equally present, and N15's scroll only let that grow.
  //
  // Fixed height, one for all three tabs. A popup anchored under a bar icon
  // grows downward, so a card fitted per tab moves the action row up and down
  // under the cursor while tabbing -- and that row is the thing meant to stay
  // where it was.
  //
  // 520 wide, which is wider than every first-party panel: `Reload config`
  // overflowed a 420 card before any of this, and the labels are sized by the
  // user's font and space scale, which this plugin does not control.
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keys
    contentWidth: panel.fittedContentWidth(Style.space(520))
    contentHeight: panel.fittedContentHeight(Style.space(520), Style.space(520))

    PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      onCloseRequested: root.close()
      // Still the shell's binding: Tab moves to the next panel on the bar, in
      // this panel as in every other. Tabs here are `[` and `]`.
      onTabRequested: function (direction) { root.switchPanel(direction) }
      onMoveRequested: function (dx, dy) { root.moveCursor(dx, dy) }
      onActivateRequested: root.activateCursor()
      onTextKey: function (t) {
        if (t === "[")
          root.stepTab(-1)
        else if (t === "]")
          root.stepTab(1)
      }

      Item {
        anchors.fill: parent

        // A parked call has a deadline, and this is the only surface carrying
        // Deny. Above the strip rather than inside a tab: behind one, somebody
        // sitting on Log never sees the question their agent is waiting on.
        Column {
          id: approvalBand
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spacing.sm
          visible: root.asking

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
                  hasCursor: root.cursorKey === "ask:once"
                  bordered: true
                  onClicked: root.service.answer("approve")
                }

                Button {
                  text: "Always"
                  hasCursor: root.cursorKey === "ask:always"
                  bordered: true
                  onClicked: root.service.answer("always")
                }

                Button {
                  text: "Deny"
                  hasCursor: root.cursorKey === "ask:deny"
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

          PanelSeparator { width: parent.width }
        }

        // `[` and `]` drive this; the mouse clicks it. Not a stop in the focus
        // ring -- it has its own keys, and every stop in that ring should do
        // something on Enter.
        Column {
          id: strip
          anchors.top: approvalBand.visible ? approvalBand.bottom : parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.topMargin: approvalBand.visible ? Style.spacing.sm : 0
          spacing: Style.spacing.sm

          ButtonGroup {
            id: tabs
            options: root.tabOptions
            value: root.tab
            focusable: false
            cursorIndex: -1
            onChanged: function (v) { root.tab = v }
          }

          PanelSeparator { width: parent.width }
        }

        // One body per tab, each keeping its own scroll position, so coming
        // back to the Log leaves it where it was rather than at the top.
        Item {
          id: body
          anchors.top: strip.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: footer.top
          anchors.topMargin: Style.spacing.sm
          anchors.bottomMargin: Style.spacing.sm

          Flickable {
            id: summaryFlick
            anchors.fill: parent
            visible: root.tab === "Summary"
            contentWidth: width
            contentHeight: summaryColumn.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.VerticalFlick
            interactive: contentHeight > height
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            Column {
              id: summaryColumn
              width: summaryFlick.width
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

                // Beside the tools line rather than in the rules section below: this
                // file is not a permissions document, and grouping it there would say
                // it was.
                Button {
                  text: "Edit config.toml"
                  hasCursor: root.cursorKey === "edit:config"
                  onHasCursorChanged: if (hasCursor) root.reveal(this)
                  bordered: true
                  enabled: root.service !== null
                  onClicked: root.service.editFile("config")
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

              // Summary is always the tab that opens, so the one thing that
              // must not be missed is stated here in words as well as by the
              // dot on the strip. A marker says *something is over there*; this
              // says what, which is the difference between a badge and a
              // warning.
              Text {
                width: parent.width
                wrapMode: Text.WordWrap
                visible: root.rulesNeedAttention
                text: {
                  var parts = []
                  if (root.needsReview)
                    parts.push("a permissions review is waiting")
                  if (root.flaggedRules > 0)
                    parts.push(root.flaggedRules + " rule"
                             + (root.flaggedRules === 1 ? "" : "s") + " need attention")
                  if (root.canPrune)
                    parts.push(root.prunableRules + " dead rule"
                             + (root.prunableRules === 1 ? "" : "s"))
                  return parts.join(" · ") + " — Rules tab"
                }
                color: Color.urgent
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }

              // Why a guarded call may be refused without anybody being asked.
              // Here rather than under Rules: it is a fact about what the
              // daemon is doing right now, not about a rule.
                Column {
                  width: parent.width
                  spacing: 2
                  visible: root.askingSuppressed || root.coolingRoutes.length > 0

                  PanelSeparator { width: parent.width }

                  PanelSectionHeader { text: "NOT ASKING" }

                  Text {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    visible: root.askingSuppressed
                    text: root.recentPrompts + " approval prompts recently, which is enough "
                        + "that the next would be answered out of habit. No more are being "
                        + "raised for now. Allow the command once instead of approving it "
                        + "every time."
                    color: Color.urgent
                    font.family: Style.font.family
                    font.pixelSize: Style.font.bodySmall
                  }

                  Repeater {
                    model: root.coolingRoutes

                    Text {
                      width: summaryColumn.width
                      wrapMode: Text.WordWrap
                      text: "· " + modelData.route + " — answered no "
                          + modelData.refusals + "×, not asking again for "
                          + Math.max(1, Math.round(modelData.secondsLeft / 60)) + " min"
                      color: Qt.darker(Color.foreground, 1.4)
                      font.family: Style.font.family
                      font.pixelSize: Style.font.bodySmall
                    }
                  }
                }
            }
          }

          Flickable {
            id: logFlick
            anchors.fill: parent
            visible: root.tab === "Log"
            contentWidth: width
            contentHeight: logColumn.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.VerticalFlick
            interactive: contentHeight > height
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            Column {
              id: logColumn
              width: logFlick.width
              spacing: Style.spacing.md


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
                    width: logColumn.width
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
            }
          }

          Flickable {
            id: rulesFlick
            anchors.fill: parent
            visible: root.tab === "Rules"
            contentWidth: width
            contentHeight: rulesColumn.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.VerticalFlick
            interactive: contentHeight > height
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            Column {
              id: rulesColumn
              width: rulesFlick.width
              spacing: Style.spacing.md

                Column {
                  id: rulesSection
                  width: parent.width
                  spacing: Style.spacing.sm

                  //: The user's own file first: it is the one with authority over the
                  //: other, and it is read first.
                  readonly property var groups: [
                    {
                      "file": "permissions.json",
                      "which": "permissions",
                      "rows": root.ownRules,
                      "removable": false
                    },
                    {
                      "file": "permissions.local.json",
                      "which": "local",
                      "rows": root.localRules,
                      "removable": true
                    }
                  ]


                  Row {
                    width: parent.width
                    spacing: Style.spacing.controlGap

                    PanelSectionHeader { text: "RULES" }

                    Text {
                      text: {
                        if (!root.rulesRead)
                          return root.rulesLoading ? "reading…" : ""
                        if (!root.permissionsOk)
                          return "the document does not load"
                        if (root.flaggedRules === 0)
                          return "nothing needs attention"
                        return root.flaggedRules + " need attention"
                      }
                      color: root.flaggedRules > 0 || !root.permissionsOk
                        ? Color.urgent : Qt.darker(Color.foreground, 1.4)
                      font.family: Style.font.family
                      font.pixelSize: Style.font.bodySmall
                    }
                  }

                  Repeater {
                    model: rulesSection.groups

                    Column {
                      id: group
                      width: rulesSection.width
                      spacing: 2

                      required property var modelData

                      //: `permissions.json` is reference material here and its rules
                      //: are read-only, so only the ones that need attention are shown
                      //: until somebody asks for the rest. The daemon's own file is
                      //: shown whole: it is the pile that Remove exists to thin, and a
                      //: grant you no longer want is not flagged as anything.
                      property bool expanded: modelData.removable
                      readonly property var flagged: modelData.rows.filter(function (r) {
                        return root.flagOf(r)[0] !== ""
                      })
                      readonly property var shown: {
                        const rows = group.expanded ? modelData.rows : group.flagged
                        return rows.slice(0, 8)
                      }
                      readonly property int hidden: (group.expanded ? modelData.rows.length
                                                                    : group.flagged.length) - group.shown.length

                      Row {
                        width: parent.width
                        spacing: Style.spacing.controlGap

                        Text {
                          text: group.modelData.file
                          color: Color.foreground
                          font.family: Style.font.family
                          font.pixelSize: Style.font.bodySmall
                        }

                        Text {
                          text: group.modelData.rows.length
                              + (group.modelData.rows.length === 1 ? " rule" : " rules")
                          color: Qt.darker(Color.foreground, 1.4)
                          font.family: Style.font.family
                          font.pixelSize: Style.font.bodySmall
                        }

                        Button {
                          text: "Edit"
                          hasCursor: root.cursorKey === "edit:" + group.modelData.which
                          onHasCursorChanged: if (hasCursor) root.reveal(this)
                          bordered: true
                          enabled: root.service !== null
                          // Creates the file if it is not there yet, with the $schema
                          // line, so the editor validates the first rule as it is
                          // typed.
                          onClicked: root.service.editFile(group.modelData.which)
                        }
                      }

                      Repeater {
                        model: group.shown

                        Column {
                          id: ruleRow
                          width: group.width
                          spacing: 0

                          required property var modelData

                          readonly property var flag: root.flagOf(ruleRow.modelData)

                          Row {
                            width: parent.width
                            spacing: Style.spacing.controlGap

                            Text {
                              text: "· " + modelData.effect + " '" + modelData.matcher + "'"
                              color: Color.foreground
                              font.family: Style.font.family
                              font.pixelSize: Style.font.bodySmall
                            }

                            Text {
                              text: modelData.covers + (modelData.covers === 1 ? " command" : " commands")
                              color: Qt.darker(Color.foreground, 1.4)
                              font.family: Style.font.family
                              font.pixelSize: Style.font.bodySmall
                            }

                            // Grants only, and only in the daemon's own file. A live
                            // deny is a decision; it is taken back in an editor, which
                            // the button above opens.
                            Button {
                              text: "Remove"
                              hasCursor: root.cursorKey === "remove:" + ruleRow.modelData.matcher
                              onHasCursorChanged: if (hasCursor) root.reveal(this)
                              bordered: true
                              visible: group.modelData.removable && modelData.effect === "allow"
                                       && root.canRemove
                              enabled: root.service !== null
                              onClicked: root.service.revoke(modelData.effect, modelData.matcher)
                            }
                          }

                          Text {
                            width: parent.width
                            wrapMode: Text.WordWrap
                            visible: ruleRow.flag[0] !== ""
                            text: "  " + ruleRow.flag[0] + ": " + ruleRow.flag[1]
                            color: ruleRow.flag[0] === "redundant" ? Qt.darker(Color.foreground, 1.4)
                                                                   : Color.urgent
                            font.family: Style.font.family
                            font.pixelSize: Style.font.bodySmall
                          }
                        }
                      }

                      Text {
                        width: parent.width
                        wrapMode: Text.WordWrap
                        visible: group.hidden > 0
                        text: "  … and " + group.hidden + " more — run omarchy-mcpd --permissions"
                        color: Qt.darker(Color.foreground, 1.6)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                      }

                      Button {
                        text: group.expanded ? "Hide the rest" : "Show all "
                                               + group.modelData.rows.length + " rules"
                        bordered: true
                        visible: !group.modelData.removable
                                 && group.modelData.rows.length > group.flagged.length
                        onClicked: group.expanded = !group.expanded
                      }

                      Text {
                        width: parent.width
                        wrapMode: Text.WordWrap
                        visible: group.modelData.rows.length === 0 && root.rulesRead
                        text: group.modelData.removable
                          ? "  nothing granted here yet; answering \u201calways\u201d writes to it"
                          : "  no rules; guarded commands take the default"
                        color: Qt.darker(Color.foreground, 1.6)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                      }
                    }
                  }
                }

                Column {
                  width: parent.width
                  spacing: Style.spacing.sm
                  visible: root.canPrune

                  PanelSeparator { width: parent.width }

                  PanelSectionHeader { text: "DEAD RULES" }

                  Text {
                    width: parent.width
                    wrapMode: Text.WordWrap
                    text: root.prunableRules + " rule" + (root.prunableRules === 1 ? "" : "s")
                        + " in permissions.local.json match no command Omarchy ships. They "
                        + "grant nothing; a rule left over from a route that was renamed."
                    color: Qt.darker(Color.foreground, 1.4)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.bodySmall
                  }

                  // Shown before anything goes. A daemon that quietly edits a file is
                  // one the user cannot reason about, which is why N10 never prunes on
                  // its own and this needs a press.
                  Repeater {
                    model: root.review.prunable || []

                    Text {
                      width: rulesColumn.width
                      wrapMode: Text.WordWrap
                      text: "· " + modelData.effect + " '" + modelData.matcher + "'"
                      color: Qt.darker(Color.foreground, 1.4)
                      font.family: Style.font.family
                      font.pixelSize: Style.font.bodySmall
                    }
                  }

                  Button {
                    text: "Prune"
                    hasCursor: root.cursorKey === "prune"
                    onHasCursorChanged: if (hasCursor) root.reveal(this)
                    bordered: true
                    enabled: root.service !== null
                    onClicked: root.service.prune()
                  }
                }

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
                      width: rulesColumn.width
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
                      width: rulesColumn.width
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
                      width: rulesColumn.width
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
                      hasCursor: root.cursorKey === "acknowledge"
                      onHasCursorChanged: if (hasCursor) root.reveal(this)
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
            }
          }
        }

        // On every tab, and pinned: these four are what a person opens this
        // panel to press. A Flow rather than a Row because the labels are
        // sized by a font this plugin does not control -- the width makes it
        // fit, the wrap makes the failure mode two tidy rows rather than a
        // button sliced off at the frame.
        Column {
          id: footer
          anchors.bottom: parent.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          spacing: Style.spacing.sm

          PanelSeparator { width: parent.width }

          Flow {
            width: parent.width
            spacing: Style.spacing.controlGap

            Button {
              text: root.serving || root.phase !== "stopped" ? "Stop" : "Start"
              hasCursor: root.cursorKey === "action:power"
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
              hasCursor: root.cursorKey === "action:restart"
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
              hasCursor: root.cursorKey === "action:check"
              bordered: true
              enabled: root.service !== null
              onClicked: root.service.checkPermissions()
            }

            Button {
              text: "Reload config"
              hasCursor: root.cursorKey === "action:reload"
              bordered: true
              enabled: root.service !== null && root.serving
              onClicked: root.service.reloadConfig()
            }

            Button {
              text: root.copied ? "Copied" : "Copy client config"
              hasCursor: root.cursorKey === "action:copy"
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
}
