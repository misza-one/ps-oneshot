import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// PS One Shot in the Omarchy bar: the controller's battery, then per machine how many agents wait for you
// (the lightbar's red), are done (its yellow), work or idle. It all comes over one connection to the daemon's
// control socket: after `watch` the daemon sends its state line on every change (ps_oneshot/status.py), and
// the clicks go back on it as action lines.
Panel {
  id: root
  moduleName: "io.github.misza-one.ps-oneshot"
  manageIpc: false  // the handler below: Panel's own, with a bound target, never registers

  readonly property string socketPath: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/ps-oneshot/control.sock"
  // The plugin checkout, which is also the daemon's: install.sh lives at its root.
  readonly property string checkout: decodeURIComponent(String(Qt.resolvedUrl("..")).replace(/^file:\/\//, "")).replace(/\/$/, "")
  readonly property bool vertical: bar ? bar.vertical : false
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color foreground: bar ? bar.barForeground : Color.foreground
  readonly property color text: Color.popups.text
  readonly property string machineLabels: String(setting("machineLabels", "Name"))
  readonly property bool showIdle: Style.boolToken(setting("showIdle", true), true)

  property var daemon: null  // the last state line; null while the daemon doesn't answer
  property bool installed: true
  property int cursor: -1  // keyboard selection among the panel's agent rows

  readonly property bool running: daemon !== null
  readonly property var machines: running ? (daemon.machines || []) : []
  readonly property var pad: {
    var pads = running ? (daemon.pads || []) : []
    var best = pads.length ? pads[0] : null
    for (var i = 1; i < pads.length; i++)
      if (pads[i].battery !== null && (best.battery === null || pads[i].battery < best.battery)) best = pads[i]
    return best
  }
  readonly property bool paused: running && daemon.paused === true
  readonly property var rows: {
    var keys = []
    for (var i = 0; i < machines.length; i++)
      for (var j = 0; j < machines[i].agents.length; j++) keys.push(machines[i].agents[j].key)
    return keys
  }
  readonly property var barGroups: {
    var groups = []
    for (var i = 0; i < machines.length; i++) {
      var counts = shownCounts(machines[i].counts)
      if (counts.length) groups.push({ label: machineLabel(machines[i].name), counts: counts })
    }
    return groups
  }
  readonly property int waiting: {
    var n = 0
    for (var i = 0; i < machines.length; i++) n += machines[i].counts.blocked + machines[i].counts.done
    return n
  }

  function statusColor(status, base) {
    if (status === "blocked" || status === "done") return daemon && daemon.colors ? daemon.colors[status] : Color.urgent
    return status === "working" ? base : Util.alpha(base, 0.4)
  }

  function shownCounts(counts) {
    var shown = []
    var order = ["blocked", "done", "working", "idle"]
    for (var i = 0; i < order.length; i++)
      if (counts[order[i]] > 0 && (order[i] !== "idle" || root.showIdle)) shown.push({ status: order[i], count: counts[order[i]] })
    return shown
  }

  function machineLabel(name) {
    var short = String(name || "").split(".")[0]
    if (root.machineLabels === "None") return ""
    if (root.machineLabels === "Initial") return short.charAt(0).toUpperCase()
    return short
  }

  function batteryText(p) {
    if (!p || p.battery === null || p.battery === undefined) return ""
    return (p.charging ? "\u{f140b}" : "") + p.battery + "%"
  }

  function padLine(p) {
    var parts = [p.bluetooth ? "Bluetooth" : "USB"]
    if (p.charging) parts.push("charging")
    if (root.paused) parts.push(daemon.game ? "paused for a game" : "paused")
    return parts.join(" · ")
  }

  readonly property string tooltip: {
    if (!running) return installed ? "PS One Shot isn't running" : "PS One Shot isn't installed"
    var lines = [pad ? pad.name + (pad.battery !== null ? " " + pad.battery + "%" : "") : "No controller"]
    if (paused) lines.push(daemon.game ? "Paused for a game" : "Paused")
    if (waiting) lines.push(waiting + (waiting === 1 ? " agent waits" : " agents wait") + " for you")
    lines.push("Middle: agents · Right: " + (paused ? "resume" : "pause"))
    return lines.join("\n")
  }

  function send(action) {
    if (!watcher.connected) return
    watcher.write(action + "\n")
    watcher.flush()
  }

  function focusAgent(key) {
    root.close()
    root.send("herd:focus:" + key)
  }

  function moveCursor(delta) {
    if (!rows.length) return
    cursor = cursor < 0 ? (delta > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, cursor + delta))
  }

  function start() {
    if (installed) root.bar.run("systemctl --user start ps-oneshot.service")
    else root.bar.run("omarchy-launch-floating-terminal-with-presentation " + root.bar.shellQuote(root.checkout + "/install.sh"))
    root.close()
  }

  Component.onCompleted: watcher.connected = true
  onOpenedChanged: {
    cursor = -1
    if (opened && !running) unitCheck.running = true
  }

  // omarchy-shell io.github.misza-one.ps-oneshot toggle: the panel from a key binding.
  IpcHandler {
    target: "io.github.misza-one.ps-oneshot"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
  }

  Socket {
    id: watcher
    path: root.socketPath
    connected: false
    parser: SplitParser {
      splitMarker: "\n"
      onRead: function(line) {
        try {
          root.daemon = JSON.parse(line)
        } catch (error) {
          console.warn("ps-oneshot: unreadable status line")
        }
      }
    }
    onConnectedChanged: {
      if (connected) {
        write("watch\n")
        flush()
      } else {
        root.daemon = null
      }
    }
    // No onError that sets connected = false: Socket cleans up after its own error handler, and doing it in
    // there too frees the connection twice when the daemon exits, which crashes the whole shell.
  }

  // The daemon restarts, or isn't up yet: try again until it answers.
  Timer {
    interval: 2000
    repeat: true
    running: !watcher.connected
    onTriggered: watcher.connected = true
  }

  Process {
    id: unitCheck
    command: ["systemctl", "--user", "cat", "ps-oneshot.service"]
    stdout: StdioCollector {}
    stderr: StdioCollector {}
    onExited: function(code) { root.installed = code === 0 }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    active: root.opened
    tooltipText: root.tooltip
    fixedWidth: root.vertical ? -1 : strip.implicitWidth + Style.space(8) * 2
    fixedHeight: root.vertical ? strip.implicitHeight + Style.space(6) * 2 : -1
    onPressed: function(code) {
      if (code === Qt.MiddleButton) root.send("herd:pick")
      else if (code === Qt.RightButton) root.send("app:pause")
      else root.toggle()
    }

    Behavior on fixedWidth { NumberAnimation { duration: 200; easing.type: Easing.OutCubic } }

    Grid {
      id: strip
      anchors.centerIn: parent
      columns: root.vertical ? 1 : 64
      columnSpacing: Style.space(10)
      rowSpacing: Style.space(6)
      verticalItemAlignment: Grid.AlignVCenter
      horizontalItemAlignment: Grid.AlignHCenter

      Grid {
        columns: root.vertical ? 1 : 3
        columnSpacing: Style.space(4)
        rowSpacing: Style.space(2)
        verticalItemAlignment: Grid.AlignVCenter
        horizontalItemAlignment: Grid.AlignHCenter

        Text {
          text: "\u{f0414}"  // PlayStation logo (Nerd Font)
          color: root.foreground
          opacity: root.running && root.pad && !root.paused ? 1 : 0.45
          font.family: root.fontFamily
          font.pixelSize: Style.bar.iconFont
        }
        Text {
          visible: text !== ""
          text: root.batteryText(root.pad)
          color: root.pad && root.pad.low ? (root.bar ? root.bar.urgent : Color.urgent) : root.foreground
          font.family: root.fontFamily
          font.pixelSize: root.vertical ? Style.font.caption : Style.font.body
        }
        Text {
          visible: root.paused
          text: "\u{f03e4}"  // pause
          color: root.foreground
          opacity: 0.6
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }
      }

      Repeater {
        model: root.barGroups

        delegate: Grid {
          id: group
          required property var modelData
          columns: root.vertical ? 1 : 64
          columnSpacing: Style.space(4)
          rowSpacing: Style.space(1)
          verticalItemAlignment: Grid.AlignVCenter
          horizontalItemAlignment: Grid.AlignHCenter

          Text {
            visible: text !== ""
            text: root.vertical ? group.modelData.label.charAt(0).toUpperCase() : group.modelData.label
            color: root.foreground
            opacity: 0.55
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
          Repeater {
            model: group.modelData.counts
            delegate: Text {
              required property var modelData
              text: String(modelData.count)
              color: root.statusColor(modelData.status, root.foreground)
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              font.bold: modelData.status === "blocked" || modelData.status === "done"
            }
          }
        }
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keys
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.moveCursor(dy !== 0 ? dy : dx) }
      onActivateRequested: if (root.cursor >= 0) root.focusAgent(root.rows[root.cursor])
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(12)

        // ---------- the controller: logo · name and link · battery ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight, heroPercent.implicitHeight)

          Text {
            id: heroIcon
            text: "\u{f0414}"
            color: root.text
            opacity: root.running && root.pad ? 1 : 0.45
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: heroPercent.left
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: root.running && root.pad ? root.pad.name : "PS One Shot"
              color: root.text
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
            }
            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: (!root.running ? (root.installed ? "not running" : "not installed")
                : root.pad ? root.padLine(root.pad) : "no controller connected").toUpperCase()
              color: Qt.darker(root.text, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
            }
          }

          Text {
            id: heroPercent
            text: root.pad && root.pad.battery !== null ? root.pad.battery + "%" : ""
            color: root.pad && root.pad.low ? Color.urgent : root.text
            font.family: root.fontFamily
            font.pixelSize: Style.font.displayLarge
            font.bold: true
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
          }
        }

        Item {
          visible: root.pad !== null && root.pad.battery !== null
          width: parent.width
          implicitHeight: Style.space(6)

          Rectangle {
            id: track
            anchors.fill: parent
            radius: height / 2
            color: Util.alpha(root.text, 0.12)
          }
          Rectangle {
            anchors.left: track.left
            anchors.verticalCenter: track.verticalCenter
            height: track.height
            radius: track.radius
            width: Math.max(track.height, track.width * (root.pad && root.pad.battery !== null ? root.pad.battery : 0) / 100)
            color: root.pad && root.pad.low ? Color.urgent : Color.accent
            Behavior on width { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }
          }
        }

        // ---------- not running: start it, or install it ----------
        Column {
          visible: !root.running
          width: parent.width
          spacing: Style.space(10)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            text: root.installed
              ? "The ps-oneshot service is stopped. Start it, or see its log: journalctl --user -u ps-oneshot"
              : "The daemon isn't installed yet. install.sh sets up its packages, the user service and a udev rule (asks for sudo)."
            color: Qt.darker(root.text, 1.2)
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
          Button {
            text: root.installed ? "Start" : "Install"
            iconText: root.installed ? "\u{f040a}" : "\u{f01da}"
            foreground: root.text
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            bordered: true
            onClicked: root.start()
          }
        }

        // ---------- agents by machine ----------
        PanelSeparator {
          visible: root.running
          foreground: root.text
        }

        Column {
          visible: root.running
          width: parent.width
          spacing: Style.space(8)

          PanelSectionHeader {
            text: "AGENTS"
            foreground: root.text
            fontFamily: root.fontFamily
          }

          Text {
            visible: root.machines.length === 0
            width: parent.width
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            text: root.running && root.daemon.herd ? "No agents running." : "Agent status needs Omaherd (herd off)."
            color: Qt.darker(root.text, 1.3)
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Flickable {
            id: list
            visible: root.machines.length > 0
            width: parent.width
            height: Math.min(groups.implicitHeight, Style.space(380))
            contentHeight: groups.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            Column {
              id: groups
              width: list.width
              spacing: Style.space(10)

              Repeater {
                model: root.machines

                delegate: Column {
                  id: machine
                  required property var modelData
                  width: groups.width
                  spacing: Style.space(2)

                  Row {
                    spacing: Style.space(6)
                    bottomPadding: Style.space(2)

                    Text {
                      textFormat: Text.PlainText
                      text: machine.modelData.name
                      color: root.text
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.bodySmall
                      font.bold: true
                    }
                    Repeater {
                      model: root.shownCounts(machine.modelData.counts)
                      delegate: Text {
                        required property var modelData
                        text: String(modelData.count)
                        color: root.statusColor(modelData.status, root.text)
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        font.bold: true
                      }
                    }
                  }

                  Repeater {
                    model: machine.modelData.agents

                    delegate: Item {
                      id: row
                      required property var modelData
                      readonly property int position: root.rows.indexOf(modelData.key)
                      readonly property bool selected: root.cursor === position
                      width: machine.width
                      implicitHeight: Style.spacing.popupRowHeight

                      onSelectedChanged: if (selected) {
                        var top = row.mapToItem(groups, 0, 0).y
                        if (top < list.contentY) list.contentY = top
                        else if (top + row.height > list.contentY + list.height) list.contentY = top + row.height - list.height
                      }

                      Rectangle {
                        anchors.fill: parent
                        radius: Style.cornerRadius
                        color: row.selected || hover.containsMouse ? Style.hoverFillFor(root.text, Color.accent) : "transparent"
                      }

                      Rectangle {
                        id: dot
                        anchors.left: parent.left
                        anchors.leftMargin: Style.space(8)
                        anchors.verticalCenter: parent.verticalCenter
                        width: Style.space(row.modelData.status === "blocked" || row.modelData.status === "done" ? 8 : 6)
                        height: width
                        radius: width / 2
                        color: root.statusColor(row.modelData.status, root.text)
                      }

                      Text {
                        id: project
                        anchors.left: parent.left
                        anchors.leftMargin: Style.space(24)
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.min(implicitWidth, parent.width * 0.4)
                        textFormat: Text.PlainText
                        text: row.modelData.project
                        color: root.text
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.body
                        elide: Text.ElideRight
                      }

                      Text {
                        anchors.left: project.right
                        anchors.leftMargin: Style.space(8)
                        anchors.right: badge.left
                        anchors.rightMargin: Style.space(8)
                        anchors.verticalCenter: parent.verticalCenter
                        textFormat: Text.PlainText
                        text: row.modelData.caption
                        color: Qt.darker(root.text, 1.4)
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        elide: Text.ElideRight
                      }

                      Text {
                        id: badge
                        anchors.right: parent.right
                        anchors.rightMargin: Style.space(8)
                        anchors.verticalCenter: parent.verticalCenter
                        text: row.modelData.badge
                        color: root.statusColor(row.modelData.status, root.text)
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: true
                        font.letterSpacing: 1.0
                      }

                      MouseArea {
                        id: hover
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onEntered: root.cursor = row.position
                        onClicked: root.focusAgent(row.modelData.key)
                      }
                    }
                  }
                }
              }
            }
          }
        }

        // ---------- what the pad's PS, Create and PS-hold do, from here ----------
        Row {
          visible: root.running
          spacing: Style.space(6)

          Button {
            text: "Agents"
            iconText: "\u{f06a9}"
            foreground: root.text
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            bordered: true
            onClicked: {
              root.close()
              root.send("herd:pick")
            }
          }
          Button {
            text: root.paused ? "Resume" : "Pause"
            iconText: root.paused ? "\u{f040a}" : "\u{f03e4}"
            foreground: root.text
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            bordered: true
            active: root.paused
            onClicked: root.send("app:pause")
          }
          Button {
            text: "Cheat sheet"
            iconText: "\u{f030c}"
            foreground: root.text
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            bordered: true
            onClicked: {
              root.close()
              root.send("ui:cheatsheet")
            }
          }
        }
      }
    }
  }
}
