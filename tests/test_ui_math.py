from __future__ import annotations

import math
import os
import tempfile
import threading
import unittest

from gi.repository import GLib

from ps_oneshot import agentcards, agentpicker, config
from ps_oneshot.agentcarousel import AgentCarousel, CarouselHooks
from ps_oneshot.control import ControlServer, send
from ps_oneshot.herd import Agent
from ps_oneshot.menus import RadialMenus
from ps_oneshot.sources import Choice
from ps_oneshot.ui.theme import FALLBACK
from ps_oneshot.ui.radial import GroupSpan, _row_width, group_spans, marquee_offset, slice_for_vector


def agent(key: str, status: str = "idle", since: int = 0, host: str = "local", title: str = "") -> Agent:
    return Agent(key=key, project="proj", kind="claude", status=status, title=title, pane_id="p", session="s",
                 host=host, hostname="box" if host != "local" else "", since=since)


def at(degrees: float, magnitude: float = 1.0) -> tuple[float, float]:
    """Stick vector at a clock angle: 0° = 12 o'clock, growing clockwise, +y down."""
    rad = math.radians(degrees)
    return math.sin(rad) * magnitude, -math.cos(rad) * magnitude


class SliceForVectorTest(unittest.TestCase):
    def test_deadzone(self) -> None:
        self.assertIsNone(slice_for_vector(0.0, 0.0, 8, 0.0))
        self.assertIsNone(slice_for_vector(*at(90, 0.29), 8, 0.3))
        self.assertEqual(slice_for_vector(*at(90, 0.31), 8, 0.3), 2)
        self.assertEqual(slice_for_vector(*at(90, 1.0), 8, 0.3), 2)

    def test_no_slices(self) -> None:
        self.assertIsNone(slice_for_vector(0.0, -1.0, 0, 0.2))

    def test_slice_zero_is_at_twelve_oclock(self) -> None:
        for count in (1, 2, 3, 8, 12):
            self.assertEqual(slice_for_vector(0.0, -1.0, count, 0.2), 0)

    def test_clockwise_order_with_eight(self) -> None:
        self.assertEqual(
            [slice_for_vector(*at(index * 45), 8, 0.2) for index in range(8)],
            list(range(8)),
        )
        # Screen axes: right is slice 2, down is 4, left is 6.
        self.assertEqual(slice_for_vector(1.0, 0.0, 8, 0.2), 2)
        self.assertEqual(slice_for_vector(0.0, 1.0, 8, 0.2), 4)
        self.assertEqual(slice_for_vector(-1.0, 0.0, 8, 0.2), 6)

    def test_boundaries_with_eight(self) -> None:
        # Slice 0 spans -22.5°..22.5°.
        self.assertEqual(slice_for_vector(*at(22.4), 8, 0.2), 0)
        self.assertEqual(slice_for_vector(*at(22.6), 8, 0.2), 1)
        self.assertEqual(slice_for_vector(*at(67.4), 8, 0.2), 1)
        self.assertEqual(slice_for_vector(*at(67.6), 8, 0.2), 2)

    def test_wraparound_near_twelve(self) -> None:
        self.assertEqual(slice_for_vector(*at(-1), 8, 0.2), 0)
        self.assertEqual(slice_for_vector(*at(359), 8, 0.2), 0)
        self.assertEqual(slice_for_vector(*at(-22.4), 8, 0.2), 0)
        self.assertEqual(slice_for_vector(*at(-22.6), 8, 0.2), 7)
        self.assertEqual(slice_for_vector(*at(-14.9), 12, 0.2), 0)
        self.assertEqual(slice_for_vector(*at(-15.1), 12, 0.2), 11)

    def test_twelve(self) -> None:
        self.assertEqual(
            [slice_for_vector(*at(index * 30), 12, 0.2) for index in range(12)],
            list(range(12)),
        )
        self.assertEqual(slice_for_vector(*at(14.9), 12, 0.2), 0)
        self.assertEqual(slice_for_vector(*at(15.1), 12, 0.2), 1)
        self.assertEqual(slice_for_vector(*at(345.1), 12, 0.2), 0)

    def test_single_slice_takes_every_direction(self) -> None:
        for degrees in range(0, 360, 15):
            self.assertEqual(slice_for_vector(*at(degrees), 1, 0.2), 0)

    def test_magnitude_beyond_unit_still_selects(self) -> None:
        self.assertEqual(slice_for_vector(1.0, 1.0, 8, 0.2), 3)


class MarqueeTest(unittest.TestCase):
    def test_rests_slides_rests_and_slides_back(self) -> None:
        # 40 px too wide at 20 px/s, 1 s rests: a 6 s cycle
        def at_time(t: float) -> float:
            return marquee_offset(40.0, 20.0, t, pause=1.0)

        self.assertEqual(at_time(0.5), 0.0)  # resting at the start
        self.assertAlmostEqual(at_time(2.0), 20.0)  # halfway out
        self.assertEqual(at_time(3.5), 40.0)  # resting at the end, the last letter in view
        self.assertAlmostEqual(at_time(5.0), 20.0)  # halfway back
        self.assertAlmostEqual(at_time(6.5), 0.0)  # next cycle
        self.assertEqual(marquee_offset(-5.0, 20.0, 2.0), 0.0)  # fits: never moves


class RowWidthTest(unittest.TestCase):
    """Rows of text in a slice of a ring with inner radius 0.42 and outer radius 1."""

    def test_top_slice_is_symmetric_between_its_edges(self) -> None:
        span = math.radians(45)
        # Both straight edges cross y = -0.71 at x = ±0.71 · tan(22.5°).
        expected = 2 * 0.71 * math.tan(span / 2)
        self.assertAlmostEqual(_row_width(0.0, -0.71, -math.pi / 2, span, 0.42, 1.0), expected, places=6)

    def test_slanted_slice_row_is_cut_by_the_rim(self) -> None:
        # Lower-left slice of six (120°..180° from +x, clockwise): the rim is nearest on the left.
        mid, span, y = math.radians(150), math.radians(60), 0.355
        to_rim = math.sqrt(1 - y * y) - 0.61
        self.assertAlmostEqual(_row_width(-0.61, y, mid, span, 0.42, 1.0), 2 * to_rim, places=6)
        # Lower down the rim comes closer: the row gets shorter.
        self.assertLess(_row_width(-0.61, y + 0.1, mid, span, 0.42, 1.0), 2 * to_rim)

    def test_outside_the_slice_has_no_room(self) -> None:
        self.assertEqual(_row_width(0.0, -0.71, math.pi / 2, math.radians(45), 0.42, 1.0), 0.0)  # bottom slice
        self.assertEqual(_row_width(0.0, -1.2, -math.pi / 2, math.radians(45), 0.42, 1.0), 0.0)  # past the rim


class GroupSpansTest(unittest.TestCase):
    def test_empty_and_ungrouped(self) -> None:
        self.assertEqual(group_spans([]), [])
        self.assertEqual(group_spans(["", "", ""]), [])

    def test_contiguous_runs(self) -> None:
        self.assertEqual(
            group_spans(["local", "local", "srv", "", "srv", "nas"]),
            [GroupSpan(0, 2, "local"), GroupSpan(2, 1, "srv"), GroupSpan(4, 1, "srv"), GroupSpan(5, 1, "nas")],
        )

    def test_single_item_groups(self) -> None:
        self.assertEqual(group_spans(["a"]), [GroupSpan(0, 1, "a")])
        self.assertEqual(group_spans(["a", "b"]), [GroupSpan(0, 1, "a"), GroupSpan(1, 1, "b")])

    def test_whole_ring(self) -> None:
        self.assertEqual(group_spans(["Output"] * 12), [GroupSpan(0, 12, "Output")])

    def test_wraparound_merges_ends(self) -> None:
        self.assertEqual(
            group_spans(["a", "b", "b", "a", "a"]),
            [GroupSpan(1, 2, "b"), GroupSpan(3, 3, "a")],
        )
        # An ungrouped item at either end breaks the circle.
        self.assertEqual(
            group_spans(["a", "b", "a", ""]),
            [GroupSpan(0, 1, "a"), GroupSpan(1, 1, "b"), GroupSpan(2, 1, "a")],
        )


class AgentPickerTest(unittest.TestCase):
    def test_cards_by_machine_opening_on_the_most_urgent(self) -> None:
        agents = [agent("w", "working", 3, host="srv"), agent("i"), agent("d", "done", 9), agent("b", "blocked", 5)]
        self.assertEqual([a.key for a in agentcards.card_order(agents)], ["i", "d", "b", "w"])
        self.assertEqual(agentcards.most_urgent(agents).key, "b")
        older_done = [agent("d1", "done", 9), agent("d2", "done", 4), agent("w", "working", 1)]
        self.assertEqual(agentcards.most_urgent(older_done).key, "d2")
        self.assertEqual(agentcards.most_urgent([agent("i", since=0), agent("w", "working", 7)]).key, "w")

    def test_card_names_are_distinct_and_path_safe(self) -> None:
        agents = [agent("a", title="π ⠏ Fix a/b"), agent("b", title="π ⠏ Fix a/b"), agent("c", host="srv")]
        self.assertEqual(agentpicker.card_names(agents), ["proj Fix a b claude", "proj Fix a b claude 2", "proj claude box"])

    def test_waited(self) -> None:
        now = 1_000_000.0
        self.assertEqual(agentcards.waited(0, now), "")
        self.assertEqual(agentcards.waited(int((now - 45) * 1000), now), "45s")
        self.assertEqual(agentcards.waited(int((now - 3 * 3600 - 300) * 1000), now), "3h 5m")
        self.assertEqual(agentcards.waited(int((now - 2 * 3600) * 1000), now), "2h")

    def test_draws_a_card(self) -> None:
        lights = config.build(config._default("base"), "hyprland", []).section("lights")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "card.png")
            agentcards.draw_card(path, agent("a", "blocked", title="A long task title " * 8), FALLBACK, lights)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")


class FakeCarouselView:
    theme = FALLBACK

    def __init__(self) -> None:
        self.on_key = None
        self.visible = False
        self.items: list = []
        self.selection = -1
        self.texts: dict = {}

    def reload_theme(self) -> None:
        pass

    def show(self, items, selection, monitor, **texts) -> None:
        self.visible = True
        self.update(items, selection, **texts)

    def update(self, items, selection, **texts) -> None:
        self.items, self.selection, self.texts = items, selection, texts

    def hide(self) -> None:
        self.visible = False

    @property
    def selected_key(self) -> str:
        return self.items[self.selection].key.split("\0")[0]


class FakeHerd:
    def __init__(self, agents: list[Agent]) -> None:
        self.agents = agents
        self.focused: list[Agent] = []

    def focus(self, agent: Agent) -> None:
        self.focused.append(agent)


class AgentCarouselTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config.build(config._default("base"), "hyprland", [])
        self.view = FakeCarouselView()
        self.notes: list[str] = []
        self.rumbles: list = []
        self.agents = [
            agent("idle", "idle", 1), agent("work", "working", 2), agent("done", "done", 9),
            agent("wait", "blocked", 5, host="srv", title="π ⠏ Deploy prod"),
        ]
        self.herd = FakeHerd(self.agents)
        self.carousel = AgentCarousel(self.cfg, self.view, self.herd, CarouselHooks(
            rumble=self.rumbles.append, monitor=lambda: None, capture_sticks=lambda: None, notify=self.notes.append,
        ))

    def key(self, name: str, char: str = "") -> None:
        self.view.on_key(name, char, True, False)

    def test_opens_on_the_most_urgent_with_blinking_waiters(self) -> None:
        self.carousel.open()
        self.assertTrue(self.view.visible)
        self.assertEqual(self.view.selected_key, "wait")
        self.assertEqual([i.blink for i in self.view.items], [False, False, True, True])

    def test_no_agents_notifies_instead(self) -> None:
        self.herd.agents = []
        self.carousel.open()
        self.assertFalse(self.view.visible)
        self.assertEqual(self.notes, ["No agents running"])

    def test_moves_wrap_and_l1_r1_jump_between_waiters(self) -> None:
        self.carousel.open()
        self.carousel.on_button("dpad_right")
        self.assertEqual(self.view.selected_key, "idle")  # wrapped past the end
        self.carousel.on_button("r1")
        self.assertEqual(self.view.selected_key, "done")
        self.carousel.on_button("r1")
        self.assertEqual(self.view.selected_key, "wait")
        self.carousel.on_button("l1")
        self.assertEqual(self.view.selected_key, "done")

    def test_typing_filters_and_escape_clears_then_closes(self) -> None:
        self.carousel.open()
        for char in "depl":
            self.key(char, char)
        self.assertEqual([i.key.split("\0")[0] for i in self.view.items], ["wait"])
        self.assertEqual(self.view.texts["filter_text"], "depl")
        self.key("x", "x")
        self.assertEqual(self.view.items, [])
        self.assertEqual(self.view.texts["empty_text"], "No matches")
        self.key("BackSpace")
        self.key("Escape")
        self.assertEqual(len(self.view.items), 4)
        self.assertTrue(self.view.visible)
        self.key("Escape")
        self.assertFalse(self.view.visible)

    def test_refresh_follows_the_selected_agent(self) -> None:
        self.carousel.open()
        self.carousel.on_button("dpad_left")
        self.assertEqual(self.view.selected_key, "done")
        self.carousel.refresh([self.agents[2], agent("new", "working", 1), *self.agents[:2]])
        self.assertEqual(self.view.selected_key, "done")

    def test_pick_closes_then_focuses(self) -> None:
        self.carousel.open()
        self.key("Return")
        self.assertFalse(self.view.visible)
        context = GLib.MainContext.default()
        deadline = GLib.get_monotonic_time() + 1_000_000
        while not self.herd.focused and GLib.get_monotonic_time() < deadline:
            context.iteration(False)
        self.assertEqual([a.key for a in self.herd.focused], ["wait"])


class ControlSocketTest(unittest.TestCase):
    def test_actions_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "control.sock")
            seen: list[str] = []
            server = ControlServer(lambda action: seen.append(action) or (None if action != "bad" else "nope"), path)
            self.assertTrue(server.start())
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            replies: list = []
            client = threading.Thread(target=lambda: replies.extend([send("herd:pick", path), send("bad", path)]))
            client.start()
            context = GLib.MainContext.default()
            while client.is_alive():
                context.iteration(False)
            self.assertEqual(replies, ["ok", "error: nope"])
            self.assertEqual(seen, ["herd:pick", "bad"])
            self.assertFalse(ControlServer(lambda action: None, path).start())  # a second daemon stays out
            server.stop()
            self.assertFalse(os.path.exists(path))
            self.assertIsNone(send("herd:pick", path))


class ChoiceSubmenuTest(unittest.TestCase):
    def test_a_choice_with_a_source_opens_it_as_a_submenu(self) -> None:
        lightbar, toggle = RadialMenus._choice_slices([
            Choice(label="Lightbar", action="", source="lights"),
            Choice(label="Sounds", action="sounds:toggle", keep_open=True),
        ])
        self.assertEqual((lightbar.source, lightbar.action, lightbar.item.detail), ("lights", "", "›"))
        self.assertEqual((toggle.source, toggle.action, toggle.keep_open), (None, "sounds:toggle", True))

    def test_a_ring_can_use_the_lights_source(self) -> None:
        base = config._default("base")
        config.build(config.deep_merge(base, {"radial": {"x": {"items": [{"label": "L", "source": "lights"}]}}}),
                     "hyprland", [])


if __name__ == "__main__":
    unittest.main()

