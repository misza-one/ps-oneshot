#!/bin/sh
# Install PS One Shot (ps-oneshot) for the current user from this checkout.
#   ./install.sh             the minimum: required packages, launcher, systemd user service, udev rule (sudo)
#   ./install.sh --full      the same plus the optional tools, each of which brings one feature
#   ./install.sh --no-udev   skip the udev rule (lightbar/player LEDs stay read-only); combines with --full
#   ./install.sh --uninstall
set -eu

here=$(cd "$(dirname "$0")" && pwd)
bin="$HOME/.local/bin/ps-oneshot"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
rule=/etc/udev/rules.d/70-ps-oneshot.rules
modules=/etc/modules-load.d/ps-oneshot.conf

full=0
udev=1
uninstall=0
for arg in "$@"; do
  case "$arg" in
    --full) full=1 ;;
    --no-udev) udev=0 ;;
    --uninstall) uninstall=1 ;;
    *) echo "usage: ./install.sh [--full] [--no-udev] | ./install.sh --uninstall" >&2; exit 2 ;;
  esac
done

if [ "$uninstall" = 1 ]; then
  systemctl --user disable --now ps-oneshot.service 2>/dev/null || true
  rm -f "$bin" "$unit_dir/ps-oneshot.service"
  systemctl --user daemon-reload
  if { [ -f "$rule" ] || [ -f "$modules" ]; } && ! { sudo rm -f "$rule" "$modules" && sudo udevadm control --reload; }; then
    echo "ps-oneshot removed, but the udev rule is still there: sudo rm $rule $modules" >&2
    exit 1
  fi
  echo "ps-oneshot removed (config in ~/.config/ps-oneshot kept)"
  exit 0
fi

# Arch packages. Required: Python's GTK bindings and the overlays. Optional, by what each brings: sound
# effects (pipewire-audio: pw-play), the audio devices ring and mic mute LED (libpulse: pactl), volume and mic
# mute keys (wireplumber: wpctl), notifications (libnotify), moving a plugged-in controller from Bluetooth to
# the cable (bluez-utils), sound effects on the controller over Bluetooth (opus). Without Omarchy, the default
# bindings also call playerctl and brightnessctl, and the menus want a Nerd Font (Omarchy ships one).
required="python-gobject gtk4 gtk4-layer-shell"
optional="pipewire-audio libpulse wireplumber libnotify bluez-utils opus"
command -v omarchy >/dev/null 2>&1 || optional="$optional playerctl brightnessctl ttf-nerd-fonts-symbols"
packages=$required
[ "$full" = 0 ] || packages="$packages $optional"

if command -v pacman >/dev/null 2>&1; then
  missing=$(pacman -T $packages || true)  # the ones not installed yet
  if [ -n "$missing" ]; then
    echo "Installing" $missing "(needs sudo) ..."
    sudo pacman -S --needed $missing
  fi
elif [ "$full" = 1 ]; then
  echo "Not Arch: install the optional tools with your package manager:" \
    "PipeWire (pw-play, pactl, wpctl), libnotify, BlueZ utilities, libopus, playerctl, brightnessctl, a Nerd Font"
fi

python3 -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Gtk4LayerShell', '1.0')" 2>/dev/null \
  || { echo "missing PyGObject, GTK 4 or gtk4-layer-shell: install them with your package manager"; exit 1; }

mkdir -p "$(dirname "$bin")" "$unit_dir"
# Bytecode goes to the cache, not the checkout: as an Omarchy plugin, the checkout is in a directory the shell
# watches, and every new file there reloads its plugins.
cat > "$bin" <<EOF
#!/bin/sh
export PYTHONPYCACHEPREFIX="\${XDG_CACHE_HOME:-\$HOME/.cache}/ps-oneshot/pycache"
PYTHONPATH="$here\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m ps_oneshot "\$@"
EOF
chmod +x "$bin"
install -m644 "$here/packaging/ps-oneshot.service" "$unit_dir/ps-oneshot.service"
systemctl --user daemon-reload

if [ "$udev" = 1 ] && ! cmp -s "$here/packaging/70-ps-oneshot.rules" "$rule" 2>/dev/null; then
  echo "Installing udev rule (needs sudo) ..."
  sudo install -m644 "$here/packaging/70-ps-oneshot.rules" "$rule"
  sudo udevadm control --reload
  sudo udevadm trigger --action=add --subsystem-match=leds
  sudo udevadm trigger --subsystem-match=hidraw --subsystem-match=misc --subsystem-match=usb --subsystem-match=input
fi

# uinput is a module on most kernels and isn't loaded until something asks: until then /dev/uinput is a
# static node the udev rule never reaches. Load it now and at every boot (built into the kernel: a no-op).
if [ "$udev" = 1 ] && [ ! -f "$modules" ] \
  && ! grep -qs '/uinput\.ko' "/lib/modules/$(uname -r)/modules.builtin"; then
  echo "Loading the uinput module (needs sudo) ..."
  echo uinput | sudo tee "$modules" >/dev/null
  sudo modprobe uinput
fi

systemctl --user enable --now ps-oneshot.service
systemctl --user restart ps-oneshot.service
echo "ps-oneshot installed and running. Logs: journalctl --user -u ps-oneshot -f"
if [ "$full" = 1 ]; then  # not in the Arch repositories
  command -v voxtype >/dev/null 2>&1 || echo "Dictation needs VoxType: https://voxtype.io"
  command -v herdr >/dev/null 2>&1 \
    || echo "Agent status needs Omaherd and herdr: https://github.com/salemsayed/omaherd, https://github.com/herdrdev/herdr"
fi
"$bin" check || true
