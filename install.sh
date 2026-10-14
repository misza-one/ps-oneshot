#!/bin/sh
# Install PS One Shot (ps-oneshot) for the current user from this checkout.
#   ./install.sh            launcher + systemd user service + udev rule (asks for sudo)
#   ./install.sh --no-udev  skip the udev rule (lightbar/player LEDs stay read-only)
#   ./install.sh --uninstall
set -eu

here=$(cd "$(dirname "$0")" && pwd)
bin="$HOME/.local/bin/ps-oneshot"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
rule=/etc/udev/rules.d/70-ps-oneshot.rules

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now ps-oneshot.service 2>/dev/null || true
  rm -f "$bin" "$unit_dir/ps-oneshot.service"
  systemctl --user daemon-reload
  if [ -f "$rule" ] && ! { sudo rm -f "$rule" && sudo udevadm control --reload; }; then
    echo "ps-oneshot removed, but the udev rule is still there: sudo rm $rule" >&2
    exit 1
  fi
  echo "ps-oneshot removed (config in ~/.config/ps-oneshot kept)"
  exit 0
fi

python3 -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Gtk4LayerShell', '1.0')" 2>/dev/null \
  || { echo "missing PyGObject, GTK 4 or gtk4-layer-shell (Arch: pacman -S python-gobject gtk4 gtk4-layer-shell)"; exit 1; }

mkdir -p "$(dirname "$bin")" "$unit_dir"
cat > "$bin" <<EOF
#!/bin/sh
PYTHONPATH="$here\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m ps_oneshot "\$@"
EOF
chmod +x "$bin"
install -m644 "$here/packaging/ps-oneshot.service" "$unit_dir/ps-oneshot.service"
systemctl --user daemon-reload

if [ "${1:-}" != "--no-udev" ] && ! cmp -s "$here/packaging/70-ps-oneshot.rules" "$rule" 2>/dev/null; then
  echo "Installing udev rule (needs sudo) ..."
  sudo install -m644 "$here/packaging/70-ps-oneshot.rules" "$rule"
  sudo udevadm control --reload
  sudo udevadm trigger --action=add --subsystem-match=leds
  sudo udevadm trigger --subsystem-match=hidraw --subsystem-match=misc
fi

systemctl --user enable --now ps-oneshot.service
systemctl --user restart ps-oneshot.service
echo "ps-oneshot installed and running. Logs: journalctl --user -u ps-oneshot -f"
"$bin" check || true
