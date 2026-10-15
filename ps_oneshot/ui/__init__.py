"""GTK 4 layer-shell overlays: radial menu and cheat sheet."""

import os

import gi

# GTK's default Vulkan renderer shows these overlays at 6-11 fps on an Intel iGPU (Meteor Lake, Arc
# graphics, 2880x1920 at scale 2): every frame uploads the whole cairo-drawn area. GL does 38-51 fps
# there, and is just as quick on a big GPU. GSK_RENDERER set in the environment still wins.
os.environ.setdefault("GSK_RENDERER", "gl")

# Pin typelib versions before any submodule imports from gi.repository.
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_foreign("cairo")
