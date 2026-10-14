"""GTK 4 layer-shell overlays: radial menu and cheat sheet."""

import gi

# Pin typelib versions before any submodule imports from gi.repository.
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_foreign("cairo")
