# Repository Guidelines

## Project Overview

PS One Shot turns a Sony DualSense into a controller for Hyprland, with an Omarchy preset. It drives workspaces and windows, radial menus, agent status from [Omaherd](https://github.com/salemsayed/omaherd), VoxType push-to-talk dictation, adaptive triggers, lightbar/rumble/sounds and a touchpad mouse.

- **Names:** CLI and service `ps-oneshot`, package `ps_oneshot`, display name `APP_NAME = "PS One Shot"`.
- **Shape:** one Linux daemon process, MIT licensed.
- **Dependencies:** stdlib plus PyGObject only (GLib, GTK 4, gtk4-layer-shell). It is standalone: don't add other dependencies or integrations.

## Architecture & Data Flow

- **One GLib main loop** (`app.run` → `App.run`), no asyncio. All `App` state changes on the main loop. The only threads are in `herd.HerdProvider` (poll and focus) and `favorites.Favorites.pick` (waits for the picker list), and they hand results back with `GLib.idle_add`.
- **Input**
  - `pad.Pad` watches the evdev fds (`GLib.unix_fd_add_full`) and calls `App._on_button`, `_on_trigger` (analog L2/R2), `_on_stick` and `_on_lost`.
  - The mic button is read from hidraw (`linux.dualsense.MicButtonReader`).
  - The touchpad becomes pointer/scroll/clicks through `uinput.VirtualPointer`.
- **Resolution:** `App._resolve` checks layers in this order, first hit wins, and unbound buttons fall through:
  1. `menu`, while a launcher layer surface is open (`[menu]` regex, fed by Hyprland layer events),
  2. held modifier layers (`[modifiers] square = "window"`),
  3. `normal`.
- **Bindings** (`config.Binding`): `press`/`release`/`tap`/`hold`/`repeat`/`light`/`soft`/`full`/`desc`.
  - Tap vs hold is decided by `input.hold_ms`, and a hold fires on the timer, not on release.
  - `soft`/`full` (staged L2/R2) run only from the analog axis (`App._on_trigger`, `TriggerPress`). The digital `BTN_TL2`/`BTN_TR2` events are ignored for them.
- **Actions** are `kind:arg` strings run by `actions.ActionRunner.run`:
  - `wm:` portable window ops (`hypr.Hyprland.wm_payload` emits Lua `hl.dsp.*` or legacy dispatchers),
  - `dispatch:` raw dispatch,
  - `exec:` shell command via `actions.spawn` (`sh -c`),
  - `key:` uinput key combos, space-separated (`key:shift+home backspace`). A combo's last key, when it's a letter, digit or US symbol name (`actions.TYPED_KEYS`) or any other xkb keysym name (`at`, `question`), is the symbol: `ActionRunner._layout` reads our virtual keyboard's layout from Hyprland (`j/devices`: layout, variant, options, `active_layout_index`), `linux.xkb.Layout` compiles it with libxkbcommon (ctypes, cached per layout) and finds the key plus Shift/AltGr that type it (German: `slash` → Shift+7, `ctrl+z` → Ctrl + the Y key). Everything else (enter, arrows, F-keys, keypad, modifiers) stays a position. Without libxkbcommon or Hyprland's answer it falls back to US positions. The IPC call happens only for combos that end in a symbol, so stick repeats don't pay for it,
  - App builtins registered in `App.__init__`: `radial`, `herd`, `ui`, `app`, `dictation`, `sounds`, `rumble`, `agent`, `touchpad`, `zoom`, `text`, `couch`, `brightness`, `launch`.
  - `launch:<desktop id>` (`apps.launch`): focus the app's most recent window (`Hyprland.focus_window`), else run `[apps] launch` (Omarchy: `uwsm-app -- {id}`). `apps.DesktopApp.owns` matches a window class to an entry by StartupWMClass, desktop id, or a Chromium web app's URL host (`chrome-<host>__-Default`).
- **Couch mode** (`couch.CouchMode`): `enter` re-applies the monitor rule with a bigger scale (`hypr.monitor_fields` copies mode, position, colour settings; `clean_scale` rounds to a scale Hyprland accepts) and takes `[couch] bar_hide` widgets out of `bar.layout` in `~/.config/omarchy/shell.json` (written byte-compatible: indent 2, key order kept, then `omarchy-shell shell reloadConfig`). `exit` puts them back at their old indexes and reloads the Hyprland config. `[couch] enter`/`exit` actions run after the change. State is `couch` = `{instance, scales}` (on only while a monitor still shows that scale) and `couch_bar` = the taken widgets, put back by `recover()` at startup if a session ended without us.
- **Stick buttons on demand:** a stick in scroll/pointer mode acts as `lstick_*`/`rstick_*` buttons while a held modifier's layer binds them (`App._sync_stick_buttons` → `Pad.force_stick_buttons`).
- **Plugged in while on Bluetooth** (`App._switch_to_cable`, on every scan): `hid-playstation` refuses the USB HID interface as a duplicate MAC, so the controller stays on Bluetooth and its USB sound card stays silent. With exactly one Bluetooth pad and not paused, it runs `bluetoothctl disconnect <Controller.uniq>`, then, once that pad is gone, `dualsense.reattach_usb` (usbfs disconnect + connect on the interface found by `dualsense.refused_usb`), and the kernel takes the controller over USB. Done when a USB pad with the same `uniq` connects; after `CABLE_SWITCH_S`, or when it can't tell which pad, a notification says how to do it by hand. Each refused HID id is tried once (`_cable_tried`).
- **Foot follows its config** (`terminals.FootFollower`, `[foot] follow`): a Gio monitor on the foot config directory sees `foot.ini`'s font size change and sends every open foot window the difference as Ctrl + KP_Add/KP_Subtract via `Hyprland.send_key` (`hl.dsp.send_key_state` with a `window`, down then up 15 ms later; keysym names, so layout-independent, and no focus change).
- **Context sources**
  - `hypr.EventStream` (socket2): active window and layers, which drive menu mode, game auto-pause (`[game]`) and agent-window detection.
  - `HerdProvider`: Omaherd JSON for lightbar alerts, the R3 radial and the PS jump. Going to an agent (`HerdProvider._focus_sequence`): `herd.session_windows` finds the windows holding a herdr client of that host/session (from `/proc`, walking up to a Hyprland client's pid; one-agent `agent attach` windows don't count). If there is one, the most recently used is raised and `omaherd-attach --focus --no-attach` switches the pane: without `--no-attach`, omaherd opens a one-agent terminal whenever several windows show other workspaces. With none, the pane is focused and `[herd] terminal` opens a whole client. Closing an agent (`HerdProvider.close`, △ held in the ring) runs `herdr pane close` through `herd.herdr_command`: local, or `ssh -- <host> exec /bin/sh -lc '…'` on Omaherd's connection master, then re-polls so the ring drops it.
  - `sources.MicMuteWatcher`: `pactl subscribe` → mic LED.
  - `dictation.CaptureWatcher`: VoxType `post_process` tee file → `DictationLog`, used for L2 undo.
  - VoxType state file: an R2 "shot" presses Enter only once it reads `idle`.
- **Outputs**
  - `lights.LightEngine.frame` drives the sysfs LED-class lightbar and player LEDs, plus evdev FF rumble. `App.refresh_lights` holds the LED writes while `Controller.rumbling` (they would stop our rumble, see the HID invariant) and keeps ticking until it's over.
  - Trigger effects and the mic mute (LED + the built-in mic's hardware mute, `power_save_control`) go out as hidraw output reports (`dualsense.build_output_report`). The kernel toggles that mute on every mic-button press; `App._sync_mic_mute` puts it back to the system mic state.
  - Overlays: `ui.radial.RadialMenu`, `ui.carousel.Carousel` and `ui.cheatsheet.CheatSheet`. They open with their own animation: Hyprland's layer animation is off for the `ps-oneshot` namespace (`Hyprland.no_layer_animation`, set at startup and again on `configreloaded`, which drops the rule).
  - Sounds: `App._play_sound`. Wired, `sounds.play` runs `pw-play` (to the controller's sound card when it has one). Over Bluetooth, `sounds.BluetoothSpeaker`: samples at 45 kHz (`dualsense.BT_AUDIO_RATE`), Opus CBR frames of 200 bytes via `linux.opus` (ctypes libopus, optional), cached per sound and volume; a worker thread sends a speaker-routing 0x31 (`build_output_report(speaker=…, keep_rumble=True)`), then one `build_audio_report` (0x36) every 512/48000 s. A newer sound cuts off the playing one. `[sounds] volume` steps at runtime with `sounds:louder|quieter` (Effects +/− in the settings ring, whose hub shows the level through `Choice.detail`).
- **Radial menus:** `menus.RadialMenus` owns the view stack and calls back through `menus.Hooks`. Its live sources are `herd`, `audio`, `voxtype-language`, `settings` and `favorites`. Every selection change goes through `RadialMenus._sync`, which also sends `_legend` (what each button does on the selected slice) to the overlay's bottom-right panel (`RadialMenu.set_legend`). Every `_show` also sends `Hooks.battery` (`App._battery_level`: the emptiest pad's percent and charging) to the bottom-left panel (`RadialMenu.set_battery`).
- **Favorites** (`favorites.Favorites`): slots saved as state key `favorites` (entries shaped like config items, `None` = empty), seeded from `[favorites] items` until the first edit. In an open ring, a favorites slice (`Slice.favorite` = slot index) takes:
  - □ and △ only as holds of `input.hold_ms` (`_start_hold`, which asks `_hold_plan` what the button does on that slice; agent slices take △ = close too): □ pins the focused window's app (`Favorites.candidate`, looked up when the ring is built), △ empties the slot. Meanwhile `RadialMenu.set_hold` shows what's about to happen in the hub and fills a ring in the button's colour. Release (`on_release`), another button or a selection change cancels.
  - L1/R1 move the slot.
  - ✕ on an empty slot: the ring closes, `[favorites] picker` gets the choices on stdin, and the ring reopens with that slot selected.
  - Inline in a configured ring, edits rebuild it from `View.items`. App icons draw through `ui.radial.lookup_icon` (GTK icon theme → render node → cairo).
- **Agent carousel** (`herd:pick`, `agentcarousel.AgentCarousel` + `ui.carousel.Carousel`): Omarchy's image picker (the theme switcher) redrawn by us, so status dots can blink. Same geometry (768x475 card, 108x432 strips at a 78 px step, skew 28) and the theme's `[image-picker]` colours from `shell.toml` (`Theme.picker_scrim` / `picker_selected` / `picker_unselected`). Cards are in R3 order (`agentcards.card_order`, by machine) and it opens on `agentcards.most_urgent`. Cards and strips (`agentcards.paint_card` / `paint_strip`) are painted once per `CarouselItem.key` into surfaces; a frame only composites them, and frames come on a change or a blink (`BLINK_MS`). Dots: on each strip, on the card, and in a row under it (`agentcards.dot_style`; waiting agents blink). Pad: D-pad/stick ←/→ (the stick repeats), L1/R1 previous/next waiting, ✕ go to, ○/L3/R3 close. Keyboard (it holds it like the radial): arrows, Tab, Home/End, Enter, Escape; typing filters (`agentcards.matches`). `App._on_herd` refreshes it live, keeping the selected agent. `herd:pick` again closes it.
- **Control socket** (`control.ControlServer`, `$XDG_RUNTIME_DIR/ps-oneshot/control.sock`, 0600): one action per connection, `ok` or `error: …` back (`App._on_control` checks the kind and radial name). `ps-oneshot agents` (`agentpicker.run`) sends `herd:pick`; with no daemon answering it falls back to `agentpicker.pick`, the same cards as PNGs in Omarchy's own image selector (static). A second daemon (a test instance) finds the socket answering and leaves it alone, so smoke tests can't open the carousel through it: bind a pad button to `herd:pick` instead.
- **Cheat sheet:** generated from the live config (`labels.cheatsheet_sections`). Each row shows the binding's `desc`, or `actions.describe(...)` when there is none.

## Key Directories

| Path | Purpose |
|---|---|
| `ps_oneshot/` | daemon: `app.py` wiring and input state machine, config, actions, feature modules |
| `ps_oneshot/linux/` | ctypes evdev/uinput, DualSense sysfs discovery, HID output reports (no python-evdev), libxkbcommon layouts (`xkb.py`) |
| `ps_oneshot/ui/` | GTK 4 layer-shell overlays (radial, carousel, cheat sheet), Omarchy theme loading (`colors.toml` + `shell.toml`), `demo.py` |
| `ps_oneshot/defaults/` | `base.toml` (generic Hyprland, plus the grammar docs in its header), `omarchy.toml` (Omarchy overlay) |
| `tests/` | stdlib unittest, pure logic |
| `tools/` | `fake_dualsense.py`, a virtual DualSense over uinput for smoke tests; `screenshots.py`, the README pictures |
| `docs/images/` | README pictures, drawn by `tools/screenshots.py` (never real screenshots: they'd show the user's windows) |
| `packaging/` | systemd user unit, udev rule |

## Development Commands

```sh
python -m unittest discover -s tests                   # full suite: 114 tests, well under a second
python -m unittest discover -s tests -k vibration      # by name pattern
python -m unittest tests.test_core.ConfigTest          # module/class/method (works without tests/__init__.py)
ruff check --select F,E9 ps_oneshot tests tools        # lint used in practice; pyproject has no ruff config
python3 -m ps_oneshot check                            # config layering, Hyprland IPC (Lua/legacy), uinput, Omaherd, pads
python3 -m ps_oneshot devices                          # discovered pads, hidraw, LEDs, battery
python3 -m ps_oneshot -v -c /tmp/x/config.toml         # foreground run with debug logs (default command: run)
python -m ps_oneshot.ui.demo                           # overlay preview with hardcoded data (needs Wayland)
python -m tools.fake_dualsense --settle 2 tap:BTN_THUMBL sleep:1 stick:ABS_X:255 sleep:1 tap:BTN_EAST
python -m tools.screenshots                            # redraw docs/images offscreen (needs a desktop session)
./install.sh [--full] [--no-udev | --uninstall]        # packages (pacman; --full adds the optional tools), launcher, user service, udev rule and uinput module via sudo
systemctl --user restart ps-oneshot && journalctl --user -u ps-oneshot -f
```

## Code Conventions & Common Patterns

- **Language level:** Python ≥ 3.11 (`tomllib`), `from __future__ import annotations`, PEP 604 types, `collections.abc.Callable`.
- **Style:** lines up to about 120 columns. No formatter is configured, so match the surrounding code. Comments explain why. Module docstrings state the contract. Long classes use `# section ----` banners.
- **Data vs state:** frozen dataclasses hold parsed or immutable data (`Binding`, `MenuItem`, `Radial`, `Agent`, `Choice`, `Theme`). Mutable classes hold runtime state (`App`, `Held`, `TriggerPress`, `Pad`, `Config`).
- **Naming:** `snake_case`, `_private` helpers, UPPER_CASE constants that carry their unit (`SHOT_POLL_MS`, `SHOT_WAIT_S`, `MAX_SLICES`). Rumble patterns are tuples of `(strong, weak, duration_ms, pause_after_ms)` (`lights.RUMBLE_*`).
- **Logging:** `log = logging.getLogger(__name__)`.
  - `info`: lifecycle and user-visible events (`connected …`, `shot sent`).
  - `debug`: per-action traces (`action %s`), shown with `-v`.
  - `warning`: recoverable failures.
- **Errors:**
  - A bad config raises `config.ConfigError` at load, and the CLI exits 2. Unknown action kinds, sources or buttons fail here.
  - Hyprland unreachable at startup → exit 1.
  - At runtime the daemon never crashes: a missing tool, device or permission → `log.warning` and carry on.
- **GLib patterns**
  - `timeout_add` callbacks return `True` to repeat and `False` for one-shots.
  - Overlay animations run in `Overlay._animate(dt)`, which returns whether it is still busy.
  - `_draw` is cairo on the CPU, every frame: big soft fills go in `Overlay._area_css` instead (CSS on the drawing area, painted by the GPU; the radial's halo), and app icons are rendered once per size (`RadialMenu._draw_image`).
  - Fire-and-forget commands go through `actions.spawn` (new session, reaped via `child_watch_add`).
  - Blocking `subprocess.run` is allowed only with a short `timeout` (pactl/voxtype queries use 3 s) or inside a worker thread.
- **Wiring:** `App.__init__` builds everything and injects callbacks: the `ActionRunner` builtins dict, `Pad` callbacks and `menus.Hooks`. Feature modules never import `app`.
- **GI imports**
  - Use `gi.require_version(...)`, then `from gi.repository import …  # noqa: E402`.
  - `ui/__init__.py` defaults `GSK_RENDERER` to `gl` (Vulkan ran the overlays at 6-11 fps on an Intel iGPU; a value in the environment still wins), then pins Gtk 4, Gtk4LayerShell and Pango.
  - Anything that shows overlays calls `preload.ensure_layer_shell_preload()` first. It re-execs with `LD_PRELOAD=libgtk4-layer-shell.so`.
- **State**
  - Config is data (`Config.raw` plus parsed `layers`, `modifiers`, `radials`).
  - Runtime toggles persist in `$XDG_STATE_HOME/ps-oneshot/state.json` via `sounds.load_state` / `sounds.save_state` (keys `sounds`, `sounds_volume`, `rumble`, `touchpad_sensitivity`, `couch`, `couch_bar`, `favorites`). They override the config at startup.
- **Config placement**
  - Generic Hyprland defaults go in `defaults/base.toml`. Anything calling `omarchy-*`, `omarchy-shell` or VoxType goes in `defaults/omarchy.toml`.
  - Tables merge and lists replace, so overriding `radial.apps.items` replaces the whole ring.
  - A ring holds at most 12 items; slice 0 sits at 12 o'clock and the rest go clockwise.
  - Integrate with Omarchy only through its commands; never edit Omarchy's own files.

### Change together

- **New action kind:** `config.ACTION_KINDS`, its handler (`ActionRunner.run` or the `App` builtins dict), `actions.describe`, and the header comment of `base.toml`.
- **New live radial source:** `config.SOURCES`, `menus.RadialMenus._source_slices` (usually returning `sources.Choice`), `labels.SOURCE_TEXT`, and the `base.toml` header.
- **New persisted toggle:** a `load_state` / `save_state` key, the `App` handler, and `App._settings_choices` (the L3 → PS One Shot ring).
- **New optional tool:** its Arch package in `install.sh` (`optional`), `__main__.OPTIONAL_TOOLS` (or `CONFIG_TOOLS` when only the default bindings call it), and the README `## Install` table.
- **New binding field:** `config.Binding`, `_parse_binding` (allowed keys and validation), `App._press` / `_release` / `_on_trigger`, and `labels.binding_text`.
- **New control:** `config.BUTTONS`, `pad.BUTTON_CODES` (or the hat/stick mapping), and `labels.GLYPHS` + `labels.ORDER`.
- **A fill moved from `_draw` into `_area_css`:** draw it in `tools/screenshots.py` too, since offscreen pictures get no CSS (`radial.draw_halo`).
- **User-visible behaviour change:** the binding's `desc` (cheat sheet text), README `## Features` and the `## Default layout` table. Limitations go in README `## Known issues`. If the overlays or the default bindings look different, rerun `python -m tools.screenshots`.

### Invariants

- **Never grab the gamepad node;** other apps and games must still see it. Only the touchpad is grabbed, in mouse mode, while active (`Pad.set_active`).
- **HID output reports set only the fields they own** (`build_output_report`: trigger, mic-LED and mic-mute (power-save) flags, plus the speaker's volume, path and preamp when routing sound). The lightbar and player LEDs go through the LED class and rumble through FF; otherwise we fight `hid-playstation`. One bit is everyone's: HAPTICS_SELECT (flag0 bit 1, "rumble, not audio haptics"). The controller stops a running rumble on any report without it, so ours set it while `Controller.rumbling`, the audio reports always do, and the 0x36 state section carries nothing else. A full state copied there (as LinuxAudio4Dualsense5 sends) marks the trigger, rumble and mic fields valid too, and resets them with every frame.
- **Overlays never take clicks:** OVERLAY layer, empty input region. The cheat sheet's keyboard mode is NONE; the radial and the carousel (`Overlay.takes_keyboard`) hold the keyboard exclusively while open, so Escape and friends work it (`radial.KEY_BUTTONS` → `RadialMenus._on_key`, same path as the pad). Anything that runs after one closes (actions, the favorites picker, focusing an agent) waits `menus.KEYBOARD_HANDBACK_MS`, or `key:` combos would land on our surface instead of the window.
- **An open radial, carousel or cheat sheet owns the sticks.** Capture is derived (`menus.is_open or cheatsheet.visible or carousel.is_open`), so call `App._sync_stick_capture()` after changing any of them.
- **A modifier (□) is a layer only when another button is pressed first** (`Held.chord`). In that case its own tap/hold don't fire.
- **Prefer `wm:` over raw `dispatch:`** so bindings work on both Lua-configured Hyprland (≥ 0.55) and the legacy dispatchers.

## Important Files

- `ps_oneshot/__main__.py`: the CLI (`run|check|devices|agents`, `-c`, `-v`, `--version`) and its exit codes.
- `ps_oneshot/agentpicker.py`: `ps-oneshot agents`. Asks the daemon for its carousel (`control.send`); without one it polls Omaherd itself, writes the cards as PNGs (`agentcards.draw_card`) into a fresh `$XDG_RUNTIME_DIR/ps-oneshot/agents/<run>/` (Qt caches images by path), opens them through `omarchy-shell image-selector open` and focuses the pick with `HerdProvider.focus_now`.
- `ps_oneshot/agentcards.py`: what both pickers draw per agent (card, strip, status dot), their order and the most urgent agent.
- `ps_oneshot/app.py`: `App`. Wiring, layers, tap/hold/chord, staged triggers, the R2 shot (`_dictation_action`, `_shot_enter`), pause/game mode, the settings ring.
- `ps_oneshot/apps.py`: desktop entries through Gio (`installed`, `find`), window ↔ app matching, `launch`.
- `ps_oneshot/favorites.py`: the favorites slots (`place` swaps a duplicate, `shift` moves), the picker list and its worker thread.
- `ps_oneshot/config.py`: `load` (base → preset → user or `-c`), `deep_merge`, `resolve_preset` (`auto` → `omarchy` if `omarchy` is on PATH), validation.
- `ps_oneshot/defaults/base.toml`: every option, plus the action and binding grammar in its header. This is the docs source of truth.
- `ps_oneshot/defaults/omarchy.toml`: Omarchy commands, R2 talk/shoot, Options/Create holds, the L3 ring (favorites, PS One Shot ›, System ›) and its `omarchy-menu-select` picker.
- `ps_oneshot/hypr.py`: socket IPC (`$XDG_RUNTIME_DIR/hypr/<sig>/.socket*.sock`, not `hyprctl`), Lua detection, `EventStream`.
- `ps_oneshot/linux/dualsense.py`: discovery and pairing, `trigger_*` effect encoders, `build_output_report` (USB `0x02` / BT `0x31` + CRC), the mic reader.
- `ps_oneshot/ui/overlay.py`: the `Overlay` base and Pango helpers. `make_layout(lines=0)` wraps freely; `lines=N` ellipsizes after N lines.
- `ps_oneshot/ui/theme.py`: reads the current Omarchy theme, with a fallback. The idle lightbar uses the theme accent.
- `install.sh`, `packaging/ps-oneshot.service`, `packaging/70-ps-oneshot.rules`, `README.md`.

## Runtime/Tooling Preferences

- **Python and dependencies:** system Python ≥ 3.11 with distro PyGObject, GTK 4 and gtk4-layer-shell (Arch: `python-gobject gtk4 gtk4-layer-shell`). No venv and no pip dependencies.
  - `pyproject.toml` (setuptools) declares only `PyGObject>=3.42` and ships `defaults/*.toml` as package data.
  - Keep the version in sync between `pyproject.toml` and `ps_oneshot/__init__.py`.
- **The installed service runs from the checkout:** `~/.local/bin/ps-oneshot` is `PYTHONPATH=<repo> python3 -m ps_oneshot`, so edits go live on `systemctl --user restart ps-oneshot`.
  - Before restarting, run the tests, the lint and `python3 -m ps_oneshot check`.
  - After restarting, confirm `connected …` and `ps-oneshot running` in the journal with no warnings.
- **Optional desktop tools:** `pactl`, `wpctl`, `pw-play`, `notify-send`, `bluetoothctl`, `voxtype`, `omarchy-*` and `omarchy-shell` (Omaherd status). Every call to them must tolerate their absence. `ps-oneshot check` lists which are there; `install.sh --full` installs them on Arch.
- **Permissions:** `packaging/70-ps-oneshot.rules` grants uaccess on the DualSense input/hidraw nodes, its USB device node (`reattach_usb`) and `/dev/uinput`, and makes the LEDs writable. On most kernels `uinput` is a module nothing loads at boot, and the rule never reaches its static node, so `install.sh` also loads it and lists it in `/etc/modules-load.d/ps-oneshot.conf` (not when built in). Both need sudo; if you can't sudo, hand the user the commands.
- **Paths:**
  - config: `~/.config/ps-oneshot/config.toml`
  - state: `~/.local/state/ps-oneshot/state.json`
  - sound cache: `~/.cache/ps-oneshot/`
  - dictation capture: `$XDG_RUNTIME_DIR/ps-oneshot/dictation`

## Testing & QA

- **Framework and helpers:** stdlib `unittest` only (`class FooTest(unittest.TestCase)`, `test_<behaviour>`).
  - Fakes: `FakeSysfs`, `caps(...)` and `usb_report`/`bt_report` in `test_linux.py`, plus a separate `agent(...)` factory per module. Constructor bypass is fine, e.g. `Hyprland.__new__`.
  - Build configs from the shipped defaults: `config.build(config.deep_merge(config._default("base"), override), "hyprland", [])`.
- **Environment:**
  - Tests need no hardware, display or network.
  - `test_ui_math.py` imports `ps_oneshot.ui`, so the GTK 4 and gtk4-layer-shell typelibs must be installed.
  - `test_linux.py` skips some tests without `/usr/include/linux/input-event-codes.h` or when running as root.
- **What not to do in unit tests:** don't import `app`, and don't construct `Overlay`, `Pad`, `VirtualPointer` or `VirtualKeyboard` (they need Wayland or `/dev/uinput`). Keep exact-byte tests (`trigger_*`, output reports, mic reports) in step with the code.
- **Not unit-tested:** `App` input flows, `Pad`, GTK drawing, actions, sounds and Hyprland IPC. Smoke-test these on a fake pad.

### Smoke tests next to a real controller

The live service doesn't grab the gamepad, so a test instance that matched the real pad would double every action. A test instance's `key:` and Enter actions also go to whatever window is focused, and that is usually the terminal running you: ○'s default is Escape, which interrupts the agent mid-call. So: drive a fake pad only, and rebind every button you press to a marker.

- **Check for an open launcher first.** If a layer matching `[menu] namespaces` is open (Omarchy menu, walker, rofi…), the `menu` layer wins: □ becomes Backspace, ✕ Enter, the D-pad arrow keys, all typed into the user's window. Read `hyprctl layers -j` and abort if one is open.
- **Rebind buttons an overlay would swallow too.** If the radial didn't open, its D-pad, □, △ and ○ fall through to the desktop layer: focus moves, the focused window is maximized, fullscreened or closed (□/△ holds), Escape is sent. After opening an overlay, assert it is up (`action radial:…` in this run's log and a `ps-oneshot` layer) before pressing anything else.
- **Use a fresh unit name every run** (`ps1-test-$(date +%s)`). With a reused name, `journalctl -u` still holds the last run's "connected" line, so a wait for it returns before the new instance has the pad and the first presses are lost.
- **Write the test config in the same step that starts the instance.** Without its `[devices]` match the instance attaches the user's real controller: every press doubles, and on stop it switches the real pad's adaptive triggers and lightbar off (restart `ps-oneshot` to put them back). `-c` with a missing file now exits 2 instead of falling back to the defaults; keep it that way.
- **Wait while the user's own overlay is up** (a `ps-oneshot` layer before you start): they're using the pad.
- **Never let a test touch real agents.** For the R3 ring, set `[herd] enabled = true`, a `status_command` script that prints made-up agents, and stub `herdr`/`ssh`/`attach` scripts first on `PATH` that only log their arguments; △ held on an agent really closes its pane otherwise.
- **Never leave an overlay up.** The user's own pad can't close a radial or cheat sheet that a test instance opened. Stop the unit in a `finally` block, in the same step that opened the overlay.
- **Keep side effects off the desktop.** Point `PATH` at a stub `notify-send`, use lone modifiers (`shift`, `ctrl`) as stand-in key combos, and aim `[couch] monitors` at a name that doesn't exist.

```toml
# /tmp/ps1-test/config.toml. With -c it replaces only the user config; base.toml + preset still apply.
[devices]
match = "Fake Virtual Test Pad"   # the live service ignores it ("Virtual" is in the default ignore)
ignore = "ps-oneshot"             # ...and this instance accepts it

[bindings.normal]                 # every button the test presses, even inside a radial
cross = "exec:echo cross >> /tmp/ps1-test/acts"
circle = "exec:echo circle >> /tmp/ps1-test/acts"
square = { tap = "exec:echo tap >> /tmp/ps1-test/acts", hold = "exec:echo hold >> /tmp/ps1-test/acts" }
dpad_right = "exec:echo right >> /tmp/ps1-test/acts"
l3 = { tap = "radial:apps", hold = "exec:echo l3hold >> /tmp/ps1-test/acts" }
```

```sh
# systemd-run inherits the graphical session env (WAYLAND_DISPLAY, HYPRLAND_INSTANCE_SIGNATURE);
# XDG_STATE_HOME keeps test toggles out of the user's state.json.
unit=ps1-test-$(date +%s)
systemd-run --user --unit "$unit" -p WorkingDirectory="$PWD" -E XDG_STATE_HOME=/tmp/ps1-test/state \
  python3 -m ps_oneshot -v -c /tmp/ps1-test/config.toml
journalctl --user -u "$unit" -o cat    # wait for "connected Fake Virtual Test Pad"
systemctl --user stop "$unit"          # afterwards: pad.close(); rm -rf /tmp/ps1-test
```

```python
import time
from tools.fake_dualsense import FakeDualSense    # run from the repo root; needs rw /dev/uinput

pad = FakeDualSense("Fake Virtual Test Pad")
pad.press("BTN_WEST"); time.sleep(0.06); pad.release("BTN_WEST")    # □ tap
pad.stick("ABS_RZ", 255); pad.stick("ABS_RZ", 0)                     # R2 analog pull (ABS_Z = L2)
pad.stick("ABS_HAT0X", -1); pad.stick("ABS_HAT0X", 0)                # D-pad ←
print(pad.rumbles)                                                   # [(strong, weak, ms), ...]
pad.close()
```

- **Button codes:** ✕ `BTN_SOUTH`, ○ `BTN_EAST`, △ `BTN_NORTH`, □ `BTN_WEST`, L1/R1 `BTN_TL`/`BTN_TR`, L3/R3 `BTN_THUMBL`/`BTN_THUMBR`, Create `BTN_SELECT`, Options `BTN_START`, PS `BTN_MODE`.
- **Sticks:** `ABS_X`/`ABS_Y` and `ABS_RX`/`ABS_RY`, range 0–255 with 128 as centre.
- **Fake pad limits:** it has no hidraw, so no mic button, LEDs or adaptive triggers.
- **Visual checks:** overlays open on the focused monitor; capture them with `grim -o <monitor>`. Note that `ui/demo.py` uses hardcoded sections, not the real config.
