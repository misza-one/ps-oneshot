from __future__ import annotations

import math
import os
import tempfile
import unittest

from ps_oneshot import agentpicker, config
from ps_oneshot.herd import Agent
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
    def test_waiting_first_then_by_machine(self) -> None:
        agents = [agent("w", "working", host="srv"), agent("i"), agent("d", "done", 9), agent("b", "blocked", 5)]
        self.assertEqual([a.key for a in agentpicker.card_order(agents)], ["b", "d", "i", "w"])

    def test_card_names_are_distinct_and_path_safe(self) -> None:
        agents = [agent("a", title="π ⠏ Fix a/b"), agent("b", title="π ⠏ Fix a/b"), agent("c", host="srv")]
        self.assertEqual(agentpicker.card_names(agents), ["proj Fix a b claude", "proj Fix a b claude 2", "proj claude box"])

    def test_waited(self) -> None:
        now = 1_000_000.0
        self.assertEqual(agentpicker.waited(0, now), "")
        self.assertEqual(agentpicker.waited(int((now - 45) * 1000), now), "45s")
        self.assertEqual(agentpicker.waited(int((now - 3 * 3600 - 300) * 1000), now), "3h 5m")
        self.assertEqual(agentpicker.waited(int((now - 2 * 3600) * 1000), now), "2h")

    def test_draws_a_card(self) -> None:
        lights = config.build(config._default("base"), "hyprland", []).section("lights")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "card.png")
            agentpicker.draw_card(path, agent("a", "blocked", title="A long task title " * 8), agentpicker.load_theme(), lights)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
