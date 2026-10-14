# PS One Shot

Use a PlayStation 5 DualSense to drive a [Hyprland](https://hypr.land) desktop and the AI agents running on it.

<p align="center">
  <img src="docs/images/favorites.png" width="49%" alt="The L3 ring: favorite apps with their own icons, PS One Shot and System submenus, and a panel in the corner listing what each button does">
  <img src="docs/images/agents.png" width="49%" alt="The R3 ring: agents grouped by machine, each slice with its project, herdr tab and a status badge">
</p>

With it you can:

- switch workspaces and move between windows,
- open your favorite apps and everything else from radial menus you steer with a stick,
- navigate launcher menus and answer agent prompts (Enter, Escape, arrow keys),
- talk to an agent and fire the prompt off with one pull of R2 — the "one shot",
- take dictation back with the adaptive L2 trigger,
- change an agent's thinking level from the controller,
- switch the dictation language and audio devices from a radial menu,
- use the touchpad as a mouse,
- see AI-agent status on the lightbar, and feel and hear when an agent needs you.

Works on any Hyprland setup. On [Omarchy](https://omarchy.org) it also uses Omarchy's own menus and commands.

## Features

### Windows and the desktop

- **Workspaces and windows.** L1/R1 step to the previous/next workspace, empty ones included, and the D-pad moves focus. A □ tap makes the window full width (maximized: bar and gaps stay, like Omarchy's Super+Alt+F); hold □ for real fullscreen, which kicks in by itself after about half a second. Hold □ and press another button right away for the window layer: the same buttons then move the window, and on Omarchy ○ switches the workspace layout between dwindle and scrolling (Super+L; also in L3 → System → Window → Layout).
- **On-the-fly adjustments (hold ○).** While ○ is held:
  - D-pad ↑/↓ changes volume, ←/→ screen brightness (laptop backlight, or DDC/CI on external monitors via Omarchy). Hold either to keep going: a slow DDC monitor gets one update at a time, always to the latest value, so it glides instead of lagging behind;
  - the left stick ↑/↓ steps the system text size (Omarchy: the same knob as Text size in its monitor panel, for the shell, GTK apps and terminals; elsewhere GTK's text scaling), and clicking it (L3) resets it. Open foot windows (herdr's terminals) follow right away, even though foot can't reload its config: PS One Shot watches `foot.ini` and zooms each open window by the difference, without focusing it, whoever changed the size;
  - the right stick ↑/↓ zooms just the focused window, for fine-tuning one terminal or page, and clicking it (R3) resets that.

  A short ○ press is still Escape. Window zoom presses the app's own keys on the keypad (Ctrl+Keypad +/−, Ctrl+0; Kitty's Ctrl+Shift ones), which type the same on every keyboard layout. It works in terminals (including herdr's), browsers and editors; L3 → System → Window has Text bigger / smaller / reset too.
- **Couch Coding Mode.** L3 → PS One Shot → **Couch mode** scales the whole screen up by 1.5 (scale 2 → 3), so terminals, browsers, the bar and the overlays all grow for reading from across the room. The bar has less room then, so on Omarchy the `[couch] bar_hide` widgets leave it for the duration (by default Omarchy's own extras: keyboard layout, weather, updates, tray, Tailscale, agents, Bluetooth, monitor) and return to their places afterwards. Switching it off restores your monitor config. `[couch]` also sets the factor, the monitors, and actions to run once the screen has changed (the Omarchy preset restarts VoxType's waveform OSD there so it measures the new size).
- **Menu mode.** While a launcher menu is open (Omarchy menu, rofi, wofi, walker, fuzzel), the D-pad works as arrow keys, ✕ as Enter, ○ as Escape and L1/R1 as Page Up/Down. On Omarchy this covers its pickers too: the theme and background grids, clipboard history, emojis, the keybindings panel and password prompts (`[menu] namespaces`, matched against layer-shell namespaces).
- **Touchpad mouse.** One finger moves the cursor and two fingers scroll. Press the touchpad to click (two fingers pressed = right click, hold and move to drag); a light tap clicks too.
- **Stick scrolling.** The right stick scrolls smoothly, with hi-res wheel events.

### Talking to agents

- **Agent prompts.** ✕ is Enter, ○ is Escape and the left stick sends arrow keys (with auto-repeat), so you can pick options in an agent's prompt and submit it without a keyboard.
- **Talk and shoot (R2).** Hold R2 lightly to dictate (Omarchy: `voxtype`, same as F9); the lightbar pulses fast and red while you talk. Let go to just type the text. Pull through the trigger's click instead to stop and send it: the recording stops right away, and Enter goes in as soon as VoxType has finished typing (it reads VoxType's state file, so a long dictation never gets cut off). A quick pull straight through, with nothing said, just sends what's already typed, like ✕. Every shot kicks back: a hard rumble, and the trigger itself buzzes under your finger for a moment.
- **Take dictation back (L2).** The L2 trigger has a resistance point you can feel (DualSense adaptive trigger):
  - a soft press (before the click) erases the last dictated phrase; press again for the one before it,
  - a full press (through the click) erases everything dictated since the last Enter,
  - with nothing dictated left, a soft press erases a word and a full press clears the line.

  Exact erasing needs the [VoxType hook](#voxtype-dictation-undo); without it L2 still erases words and lines.
- **Thinking level (Options).** A tap sends the focused agent's own "next thinking level" key; the agent kind comes from Omaherd (omp and pi: Shift+Tab, Claude Code: Alt+T, configurable in `[agents]`). Outside an agent window, and on a hold, Options opens the Omarchy menu.
- **Agent status ([Omaherd](https://github.com/salemsayed/omaherd)).** The lightbar shows the most urgent agent: blinking red = waiting for you, blinking yellow = done, soft breathing = working. The five player LEDs count waiting agents. When an agent starts waiting you get a buzz and a beacon sound. The PS button jumps to the most urgent agent; press again within 4 s to cycle to the next one.
- **Sound effects.** A blaster "pew" when you send a prompt (✕ in an agent window, or an R2 shot), a power-up chirp on a thinking-level change, a beacon when an agent calls. All synthesized; swap any of them for your own file in `[sounds]`, and toggle them all in L3 → PS One Shot (`sounds:toggle`, remembered across restarts). They play on the controller's speaker while it's wired; see [known issues](#known-issues).

### Radial menus

<img src="docs/images/hold.png" width="42%" align="right" alt="Holding □ on an empty slot: the centre says PIN HERE, Files, and a ring fills up in the button's colour">

L3 opens your favorites, R3 opens agents, holding the mic button opens voice. Pick a slice with either stick, confirm with ✕, go back with ○. Submenus are supported, and every tick has a tiny haptic click. A name too long for its slice scrolls through it while that slice is selected. A panel in the corner of the ring lists what each button does on the selected slice.

- **Favorites** (L3) are six slots for what you open most, in fixed places so your hand learns them, next to PS One Shot › and System › (apps, window, audio, notifications, dictation, screenshot, lock). Apps show their own icons, and picking one focuses its open window instead of starting another, web apps included. You change the slots right in the ring, and it remembers them (`[favorites] items` only sets where it starts):
  - ✕ on an empty slot ("Add") picks from a list of every installed app (Omarchy's own menu, or any dmenu-style list).
  - Holding □ pins the app of the window you were in; holding △ empties a slot. While you hold, the centre names what's about to happen and a ring fills up; let go early and nothing changes.
  - L1/R1 move a favorite one slot.
- **Agents** (R3) are grouped by machine: local agents first, then each SSH host, each with its own coloured arc and hostname. Every slice shows the project, its herdr tab (or task title) and a status badge. Going to an agent (here or with PS) brings up the herdr window already showing that session, the most recently used one if there are several, and switches it to the agent. A new terminal opens only when no window shows the session yet.
- **Voice** (hold the mic button) puts the dictation languages straight on the ring (the current one checked) plus a Devices submenu with every output and input; picking a device makes it the default and moves playing streams.
- **PS One Shot** (L3) holds the app's own switches: couch mode, sound effects and vibration on/off (icon, ✓ and on/off show the current state), touchpad pointer slower/faster, the cheat sheet and pause. Toggles and the speed stay open for another press and are remembered across restarts. Vibration off silences every rumble and the shot's trigger kick; the triggers' resistance stays.

<br clear="right">

### And also

- **Theme and background (Omarchy, hold Create).** Create + ☰ opens Omarchy's theme menu, Create + □ its background switcher (the current theme's wallpapers), Create + D-pad → steps to the next background. Press the second button right away; a short Create alone is still the cheat sheet.
- **Game mode.** Hold PS to pause everything; Steam games pause the controller automatically. While paused, it leaves the LEDs and triggers alone and releases the touchpad.
- **Mic button.** A tap mutes your microphone, a hold opens the voice radial. The button's LED and the controller's own microphone follow the system microphone (lit and cut off = muted), whatever muted it. The kernel flips the controller mic's hardware mute on every press, holds included; PS One Shot puts it back, so dictating into the controller (wired) keeps working. The button is read via hidraw, because the kernel doesn't report it as an input event.
- **Cheat sheet.** Press Create to see every binding, the sticks and touchpad, and what's inside each radial menu; ○ or Create closes it. Nothing is cut short: when it's taller than the screen, either stick or D-pad ↑/↓ scrolls it, and a scrollbar shows where you are.
- **Battery.** Notifications at 20 % and 10 %, plus an orange lightbar blink when the battery is low.

## Default layout

| Control | Desktop | Holding □ | Holding ○ | In a menu |
|---|---|---|---|---|
| L1 / R1 | previous / next workspace (empty ones too) | send window to previous / next workspace | | page up / down |
| D-pad | focus window ← → ↑ ↓ | swap window ← → ↑ ↓ | ↑↓ volume, ←→ brightness | arrow keys |
| Left stick | arrow keys | | ↑↓ system text size (L3 resets) | arrow keys |
| ✕ | Enter | toggle scratchpad | | Enter |
| ○ | Escape (tap) | workspace layout dwindle ↔ scrolling (Omarchy; elsewhere maximize) | | Escape |
| □ | full width (tap) / fullscreen (hold) | | | Backspace |
| △ | close window (hold) | toggle floating | | — |
| L2 | erase dictation: soft = last phrase, full = all since Enter | | | |
| R2 | talk (hold lightly) / send (pull through the click) | | | talk / send |
| L3 / R3 | Favorites radial / Agents radial (on release); hold (Omarchy): new agent / clipboard history | | reset system text / window zoom | |
| Options | thinking level in an agent (tap) / Omarchy menu (hold) | System menu | | |
| Create | cheat sheet (tap) / Omarchy keybindings (hold); held with ☰: theme menu, □: background switcher, D-pad →: next background | | | |
| Touchpad press | left click (two fingers: right click) | | | |
| PS | jump to waiting agent (tap) / pause (hold) | | | |
| Mic | mute microphone (tap) / voice radial (hold) | | | |
| Right stick | scroll | | ↑↓ focused window's text size | |

Buttons with no binding in a layer fall back to the next layer down: menu → □/○ → desktop. That's why talking still works inside a launcher and you can dictate into its search field. □ and ○ act as modifiers only on their own; pressed while the other is held, they do that layer's job (□ held + ○ = layout).

The cheat sheet (Create) is generated from your configuration, so it always shows what your buttons do. With the Omarchy defaults:

<p align="center">
  <img src="docs/images/cheatsheet.png" width="100%" alt="The cheat sheet: every binding grouped by layer, what's inside each radial menu, and how to move around in radials and menus">
</p>

## Install

Requirements:

- Hyprland (Lua config ≥ 0.55 or the classic hyprlang one),
- Python ≥ 3.11 with PyGObject, GTK 4 and gtk4-layer-shell,
- a [Nerd Font](https://www.nerdfonts.com) for the menu icons (Omarchy ships one; without it the menus show text only).

Optional: [VoxType](https://voxtype.io) for dictation, [Omaherd](https://github.com/salemsayed/omaherd) and [herdr](https://github.com/herdrdev/herdr) for agent status, PipeWire (`pw-play`) for sound effects.

On Arch:

```sh
sudo pacman -S python-gobject gtk4 gtk4-layer-shell
git clone https://github.com/misza-one/ps-oneshot && cd ps-oneshot
./install.sh
```

`install.sh` sets up three things:

1. a `~/.local/bin/ps-oneshot` launcher,
2. a systemd user service (`ps-oneshot.service`) bound to `graphical-session.target`,
3. `packaging/70-ps-oneshot.rules`, installed with sudo.

The udev rule makes the lightbar and player LEDs writable and gives the seat user access to the controller's hidraw node (mic button, adaptive triggers) and `/dev/uinput`. Reconnect the controller once the rule is installed. Without it, everything works except the LEDs. Pass `--no-udev` to skip the rule, or `--uninstall` to remove everything.

```sh
ps-oneshot check      # config, Hyprland IPC, uinput, controller, LED permissions
ps-oneshot devices    # what was found and what is accessible
ps-oneshot -v         # run in the foreground with debug logs
journalctl --user -u ps-oneshot -f
```

## Configuration

Settings load in this order:

1. `ps_oneshot/defaults/base.toml` (generic Hyprland),
2. `ps_oneshot/defaults/omarchy.toml` (when Omarchy is detected),
3. `~/.config/ps-oneshot/config.toml`.

Tables merge and values replace. Lists replace as a whole, so overriding `radial.apps.items` means listing every item you want. All actions and binding forms are documented at the top of `base.toml`. A few examples:

```toml
[bindings.normal]
cross = "wm:cyclenext"                                     # ✕ = Alt+Tab instead of Enter
triangle = ""                                              # remove a default
l2 = { soft = "key:ctrl+z", full = "key:ctrl+a delete" }   # two-stage trigger, any actions

[bindings.window]                                          # while □ is held
dpad_up = { press = "exec:omarchy-audio-output-volume raise", repeat = true }

[sticks]
left = "pointer"                                           # left stick moves the cursor instead of arrows

[triggers]
click_zone = [3, 5]                                        # earlier, lighter click on L2/R2
click_strength = 4

[agents]
thinking = { omp = "shift+tab", claude = "alt+t", codex = "" }

[sounds]
summon = "~/sounds/r2d2.ogg"                               # any file pw-play can read; "" = silent

[voxtype]
languages = [{ code = "en", label = "English" }, { code = "pl", label = "Polski" }]

[favorites]                                                # where the L3 ring starts; then edit it in the ring
items = [
  { action = "launch:org.telegram.desktop.desktop" },      # an app: its own name and icon
  { label = "Notes", icon = "\uf249", action = "exec:obsidian" },
]

[radial.apps]
title = "Mine"
items = [
  { label = "Terminal", icon = "\uf120", action = "exec:omarchy-launch-terminal" },
  { label = "Media", icon = "\uf001", items = [
    { label = "Play/Pause", action = "exec:playerctl play-pause" },
    { label = "Next", action = "exec:playerctl next", keep_open = true },
  ] },
]
```

Radial items can also be live: `{ label = "Devices", source = "audio" }`, `source = "voxtype-language"`, `source = "herd"`, `source = "settings"` (PS One Shot's own switches) or `source = "favorites"` (the `[favorites]` slots) build a submenu when you open it, and `inline = true` puts the live choices straight on the ring.

`launch:<desktop id>` (for example `launch:org.gnome.Nautilus.desktop`) focuses that app's open window, or starts it with `[apps] launch` (`uwsm-app` on Omarchy, `gtk4-launch` elsewhere). Favorites use it for every app you add.

`wm:` actions (`workspace`, `focus`, `swap`, `movetoworkspace`, `fullscreen`, `maximize`, `float`, `pin`, `close`, `special`, `cyclenext`, `cycleprev`, `monitor`) work with both Lua-configured Hyprland (≥ 0.55) and the legacy hyprlang dispatchers. PS One Shot detects which one you run.

### VoxType dictation undo

VoxType types text like a keyboard and keeps no history, so PS One Shot needs to see each phrase to erase it exactly. VoxType's `post_process` hook passes every transcription through a shell command before typing it. Use `tee` there: the text goes through unchanged and a copy lands where PS One Shot watches for it:

```sh
voxtype config set output.post_process.command 'tee "$XDG_RUNTIME_DIR/ps-oneshot/dictation"'
systemctl --user restart voxtype
```

This isn't a native VoxType feature, so keep a few things in mind:

- If PS One Shot isn't running, the folder doesn't exist, `tee` fails, and VoxType falls back to typing the original text. Dictation still works, it just isn't recorded.
- The hook replaces any other `post_process` command, such as an LLM clean-up. To keep yours, chain it: `your-command | tee "$XDG_RUNTIME_DIR/ps-oneshot/dictation"`.
- Erasing sends one Backspace per character, scoped to the window you dictated into. If you moved the cursor or typed in between, the count no longer lines up.
- Any other dictation tool works too if it writes each phrase to `dictation.capture_file`.

## Known issues

- **Sounds only reach the controller's speaker over USB.** Linux has no audio path to a DualSense over Bluetooth (the kernel driver only does audio over USB), so on Bluetooth the sound effects play on your normal output instead. Set `[sounds] device = "controller"` to keep them silent rather than on your speakers.
- **Thinking level is cycled blind.** Agents don't publish their current thinking level, so the controller sends the agent's "next level" key and the same power-up sound plays every time.
- **OpenRGB fights over the lightbar.** If OpenRGB (or a tool built on it) also detects the controller, both write the lightbar and colours flicker or mix. Disable OpenRGB's "Sony DualSense" detector.
- **Steam Input.** With Steam running and its desktop controller configuration enabled, Steam turns the controller into a mouse/keyboard as well. Disable the desktop configuration or quit Steam.
- **Leaving couch mode reloads the Hyprland config.** That is what restores every monitor exactly, including settings a runtime rule can't read back (ICC profiles, reserved areas). The catch: changes made at runtime with `hyprctl keyword`/`eval` are reset too. Omarchy keeps its toggles and workspace layouts in files, so those survive.
- **Monitor-profile daemons undo couch mode.** hyprmoncfgd (and similar) notice the scale change within seconds and re-apply their profile. Pause them with `[couch] enter`/`exit` actions (example in `base.toml`).
- **Third-party bar widgets stop while couch mode hides them.** Omarchy only runs third-party widgets that are on the bar, so whatever a hidden one does (a radio, a chat you summon) pauses until couch mode ends. Its own `omarchy.*` widgets just hide; that's why the default `bar_hide` only lists those.
- **`key:` actions press physical keys at their US-layout positions.** With another keyboard layout, a symbol key types whatever that layout puts there; on a German layout `key:minus` types ß. Letters mostly survive (QWERTZ swaps Y and Z, AZERTY swaps more). That's why text zoom uses the keypad +/−, which are the same on every layout.
- **□ pins only windows that an installed app owns.** A window is matched to its `.desktop` entry by class: the entry's `StartupWMClass` or desktop id, or for a web app the host of its URL. Windows that share one class can't be told apart: Omarchy's TUIs all run as `TUI.float` or `TUI.tile`. Add those from the list instead; they then start a new window each time rather than focusing the open one.

## How it works

One Python process runs one GLib main loop and needs no dependencies beyond PyGObject. It reads the controller through evdev (stdlib `ioctl`, no python-evdev) and the mic button through hidraw. It drives the LEDs via the kernel `hid-playstation` LED class and rumble via force feedback. The adaptive trigger effects and the mic mute (LED plus the microphone's hardware mute) go out as raw hidraw output reports that set only their own fields, so they don't interfere with the kernel driver. Sounds are synthesized once into `~/.cache/ps-oneshot` and played with `pw-play`. Hyprland is reached over its IPC sockets. The overlays are GTK 4 layer-shell surfaces that never take keyboard focus. The game controller itself is never grabbed, so other apps still see it; only the touchpad is grabbed while PS One Shot is active.

## Development

```sh
python -m unittest discover -s tests
python -m tools.fake_dualsense --settle 2 tap:BTN_THUMBL sleep:1 stick:ABS_X:255 sleep:1 tap:BTN_EAST
python -m ps_oneshot.ui.demo
python -m tools.screenshots      # redraw the pictures in docs/images (run it in a desktop session)
```

`tools/fake_dualsense.py` creates a virtual DualSense through uinput: a gamepad plus a touchpad, and it accepts rumble. You can drive it from a script to test without hardware. `tools/screenshots.py` draws the overlays offscreen with made-up apps and agents. [AGENTS.md](AGENTS.md) describes the architecture, conventions and how to test safely next to a real controller.

## License

[MIT](LICENSE)
