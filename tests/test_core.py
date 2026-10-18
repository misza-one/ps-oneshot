import colorsys
import unittest

from ps_oneshot import config, herd, lights, tuning
from ps_oneshot.hypr import Hyprland, HyprlandError, clean_scale


def agent(key, status, since=0):
    return herd.Agent(key=key, project=key, kind="omp", status=status, title="", pane_id="p",
                      session="default", host="local", hostname="", since=since)


class ConfigTest(unittest.TestCase):
    def build(self, override):
        raw = config.deep_merge(config._default("base"), override)
        return config.build(raw, "hyprland", [])

    def test_user_override_merges_tables_and_replaces_values(self):
        cfg = self.build({"bindings": {"normal": {"cross": "wm:close"}}, "sticks": {"right": "pointer"}})
        self.assertEqual(cfg.layers["normal"]["cross"].press, "wm:close")
        self.assertIn("l1", cfg.layers["normal"])  # untouched defaults survive
        self.assertEqual(cfg.raw["sticks"]["left"], config._default("base")["sticks"]["left"])
        self.assertEqual(cfg.raw["sticks"]["right"], "pointer")

    def test_empty_string_unbinds_default(self):
        cfg = self.build({"bindings": {"normal": {"square": ""}}})
        self.assertNotIn("square", cfg.layers["normal"])

    def test_staged_bindings_only_on_triggers_and_not_mixed(self):
        with self.assertRaises(config.ConfigError):
            self.build({"bindings": {"normal": {"cross": {"soft": "dictation:back"}}}})
        with self.assertRaises(config.ConfigError):
            self.build({"bindings": {"normal": {"l2": {"soft": "dictation:back", "hold": "wm:close"}}}})
        cfg = self.build({"bindings": {"normal": {"r2": {"press": "exec:a", "release": "exec:b", "full": "exec:c"}}}})
        self.assertTrue(cfg.layers["normal"]["r2"].staged)

    def test_modifier_rejects_repeat_on_its_own_binding(self):
        with self.assertRaises(config.ConfigError):
            self.build({"bindings": {"normal": {"square": {"press": "wm:close", "repeat": True}}}})

    def test_radial_items_validate_nested_actions_and_sources(self):
        with self.assertRaises(config.ConfigError):
            self.build({"radial": {"apps": {"title": "x", "items": [
                {"label": "sub", "items": [{"label": "bad", "action": "radial:missing"}]},
            ]}}})
        with self.assertRaises(config.ConfigError):
            self.build({"radial": {"apps": {"title": "x", "items": [{"label": "s", "source": "weather"}]}}})

    def test_favorites_need_a_label_unless_launch_and_fit_the_slots(self):
        cfg = self.build({"favorites": {"items": [{"action": "launch:foot.desktop"}]}})
        self.assertEqual(cfg.raw["favorites"]["items"], [{"action": "launch:foot.desktop"}])
        with self.assertRaises(config.ConfigError):
            self.build({"favorites": {"items": [{"action": "exec:foot"}]}})
        with self.assertRaises(config.ConfigError):
            self.build({"favorites": {"slots": 1, "items": [{"label": "a", "action": "exec:a"}] * 2}})

    def test_launch_command_and_blink_period_fail_at_load_not_at_runtime(self):
        for launch in (["uwsm-app", "--", "{id}"], "gtk4-launch {id} {name}"):
            with self.assertRaises(config.ConfigError):
                self.build({"apps": {"launch": launch}})
        with self.assertRaises(config.ConfigError):
            self.build({"lights": {"blink_period": 0}})
        with self.assertRaises(config.ConfigError):
            self.build({"favorites": {"items": [{"label": 42, "action": "exec:a"}]}})

    def test_rejects_unknown_button_and_bad_radial(self):
        with self.assertRaises(config.ConfigError):
            self.build({"bindings": {"normal": {"start": "wm:close"}}})
        with self.assertRaises(config.ConfigError):
            self.build({"bindings": {"normal": {"l3": "radial:nope"}}})

    def test_press_and_tap_are_exclusive(self):
        with self.assertRaises(config.ConfigError):
            self.build({"bindings": {"normal": {"cross": {"press": "wm:close", "tap": "wm:float"}}}})

    def test_menu_item_needs_exactly_one_of_action_or_items(self):
        with self.assertRaises(config.ConfigError):
            self.build({"radial": {"apps": {"title": "x", "items": [{"label": "empty"}]}}})

    def test_omarchy_preset_parses_and_overrides_base(self):
        base = config.build(config._default("base"), "hyprland", [])
        omarchy_raw = config._default("omarchy")
        cfg = config.build(config.deep_merge(config._default("base"), omarchy_raw), "omarchy", [])
        overridden = set(omarchy_raw["bindings"]["normal"]) & set(base.layers["normal"])
        self.assertTrue(overridden)
        for button in overridden:  # preset wins over base, other base bindings survive
            self.assertEqual(cfg.layers["normal"][button], config._parse_binding(button, omarchy_raw["bindings"]["normal"][button]))
        self.assertEqual(cfg.layers["normal"]["l1"], base.layers["normal"]["l1"])


class HerdTest(unittest.TestCase):
    def test_waiting_queue_blocked_first_then_oldest(self):
        agents = [agent("a", "done", 5), agent("b", "blocked", 9), agent("c", "working"), agent("d", "blocked", 3)]
        self.assertEqual([a.key for a in herd.waiting_queue(agents)], ["d", "b", "a"])

    def test_transitions_ignore_first_sight_and_unchanged(self):
        before = {"a": "working", "b": "blocked"}
        now = [agent("a", "blocked"), agent("b", "blocked"), agent("new", "done")]
        self.assertEqual([a.key for a in herd.transitions(before, now)], ["a"])

    def test_parse_payload_tolerates_wrapping_and_noise(self):
        text = 'log line\n{"result": {"agents": [{"key": "k", "status": "BLOCKED", "agent": "Claude"}]}}'
        parsed = herd.parse_payload(text)
        self.assertEqual([(a.key, a.status, a.kind) for a in parsed], [("k", "blocked", "claude")])
        self.assertIsNone(herd.parse_payload("not json"))


class LightsTest(unittest.TestCase):
    def setUp(self):
        self.engine = lights.LightEngine(config._default("base")["lights"], (1, 2, 3))

    def test_priority_order(self):
        cfg = config._default("base")["lights"]
        cases = [
            (lights.LightInputs(recording=True, herd_status="blocked", battery_low=True), cfg["recording"]),
            (lights.LightInputs(herd_status="blocked", battery_low=True), cfg["blocked"]),
            (lights.LightInputs(herd_status="done", battery_low=True), cfg["done"]),
            (lights.LightInputs(herd_status="working", battery_low=True), cfg["battery_low"]),
            (lights.LightInputs(herd_status="working"), [1, 2, 3]),
        ]
        for inputs, expected in cases:
            self.assertEqual(self.engine.frame(inputs, 0.0).rgb, lights.vivid(tuple(expected)), inputs)

    def test_every_lightbar_colour_goes_out_vivid_unless_switched_off(self):
        self.assertEqual(lights.vivid((250, 169, 104)), (255, 114, 0))  # retro-82's peach accent → ff7200
        self.assertEqual(lights.vivid((220, 68, 68)), (255, 0, 0))  # the alert's red, not pink
        self.assertEqual(lights.vivid((128, 128, 128)), (255, 255, 255))  # no hue: stays neutral
        cfg = config._default("base")["lights"]
        engine = lights.LightEngine(cfg, (250, 169, 104))
        frames = {
            "idle": engine.frame(lights.LightInputs(), 0.0),
            "alert": engine.frame(lights.LightInputs(herd_status="blocked", alert_rgb=(224, 145, 90)), 0.1),
            "agent": engine.frame(lights.LightInputs(herd_status="blocked", alert_rgb=(224, 145, 90)), 0.6),
            "done": engine.frame(lights.LightInputs(herd_status="done"), 0.1),
            "recording": engine.frame(lights.LightInputs(recording=True), 0.0),
            "battery": engine.frame(lights.LightInputs(battery_low=True), 0.0),
        }
        for name, frame in frames.items():
            _h, saturation, value = colorsys.rgb_to_hsv(*(c / 255 for c in frame.rgb))
            self.assertEqual((round(saturation, 3), round(value, 3)), (1.0, 1.0), (name, frame.rgb))
        self.assertEqual(engine.led([20, 40, 140]), (0, 43, 255))  # the pause colour, written by the app
        as_written = lights.LightEngine({**cfg, "vivid": False}, (250, 169, 104))
        self.assertEqual(as_written.frame(lights.LightInputs(), 0.0).rgb, (250, 169, 104))
        self.assertEqual(as_written.led([20, 40, 140]), (20, 40, 140))

    def test_player_leds_count_waiting_or_scan_while_agents_work(self):
        waiting = self.engine.frame(lights.LightInputs(herd_status="blocked", waiting=2), 0.0)
        self.assertEqual(waiting.player_mask, lights.player_mask(2))
        step = lights.BREATH_PERIOD_S / len(lights.SCANNER)
        masks = [self.engine.frame(lights.LightInputs(herd_status="working"), step * (i + 0.5)) for i in range(8)]
        self.assertEqual([f.player_mask for f in masks], [1, 2, 4, 8, 16, 8, 4, 2])
        self.assertTrue(all(f.animated for f in masks))
        self.assertEqual(self.engine.frame(lights.LightInputs(), 0.0).player_mask, 0)

    def test_paused_leaves_leds_alone(self):
        self.assertIsNone(self.engine.frame(lights.LightInputs(paused=True, herd_status="blocked"), 0.0))

    def test_player_mask_fills_from_centre(self):
        self.assertEqual(lights.player_mask(0), 0)
        self.assertEqual(lights.player_mask(1), 0b00100)
        self.assertEqual(lights.player_mask(3), 0b01110)
        self.assertEqual(lights.player_mask(9), 0b11111)


class WmPayloadTest(unittest.TestCase):
    def hypr(self, lua):
        instance = Hyprland.__new__(Hyprland)
        instance._lua = lua
        return instance

    def test_lua_and_legacy_payloads(self):
        self.assertEqual(self.hypr(True).wm_payload("workspace", "e+1"), 'hl.dsp.focus({ workspace = "e+1" })')
        self.assertEqual(self.hypr(False).wm_payload("workspace", "e+1"), "workspace e+1")
        self.assertEqual(self.hypr(False).wm_payload("close"), "killactive")

    def test_lua_argument_is_quoted(self):
        payload = self.hypr(True).wm_payload("special", 'a"b')
        self.assertEqual(payload, 'hl.dsp.workspace.toggle_special("a\\"b")')
        # Lua has no \uXXXX escape: non-ASCII goes through as is, control characters as \ddd.
        payload = self.hypr(True).wm_payload("special", "café\n")
        self.assertEqual(payload, 'hl.dsp.workspace.toggle_special("café\\010")')

    def test_rejects_bad_direction_and_op(self):
        with self.assertRaises(HyprlandError):
            self.hypr(True).wm_payload("focus", "x")
        with self.assertRaises(HyprlandError):
            self.hypr(True).wm_payload("explode")


class MonitorRuleTest(unittest.TestCase):
    # j/monitors entry of a 10-bit wide-gamut 4K monitor at scale 2 (Hyprland 0.56)
    MONITOR = {
        "name": "DP-3", "width": 3840, "height": 2160, "refreshRate": 239.991, "x": 1975, "y": 121,
        "scale": 2, "transform": 0, "currentFormat": "XBGR2101010", "mirrorOf": "none",
        "colorManagementPreset": "wide", "sdrBrightness": 1, "sdrSaturation": 1,
        "sdrMinLuminance": 0.2, "sdrMaxLuminance": 80,
    }

    def hypr(self, lua):
        instance = Hyprland.__new__(Hyprland)
        instance._lua = lua
        return instance

    def test_scale_rounds_up_to_whole_logical_pixels(self):
        self.assertEqual(clean_scale(3.0, 3840, 2160), 3.0)
        self.assertEqual(clean_scale(2.9, 3840, 2160), 3.0)
        self.assertEqual(clean_scale(1.875, 1920, 1080), 1.875)  # 1024x576
        self.assertEqual(clean_scale(1.875, 2560, 1440), 2.0)  # 1365.33 wide won't do

    def test_rule_keeps_mode_position_and_colour_settings(self):
        self.assertEqual(
            self.hypr(True).monitor_payload(self.MONITOR, 3),
            'eval hl.monitor({ output = "DP-3", mode = "3840x2160@239.991", position = "1975x121", scale = 3, '
            'transform = 0, bitdepth = 10, cm = "wide", sdrbrightness = 1, sdrsaturation = 1, '
            "sdr_min_luminance = 0.2, sdr_max_luminance = 80 })",
        )
        self.assertEqual(
            self.hypr(False).monitor_payload(self.MONITOR, 2.5),
            "keyword monitor DP-3,3840x2160@239.991,1975x121,2.5,transform,0,bitdepth,10,cm,wide,"
            "sdrbrightness,1,sdrsaturation,1,sdr_min_luminance,0.2,sdr_max_luminance,80",
        )

    def test_output_name_is_quoted_for_lua(self):
        payload = self.hypr(True).monitor_payload({**self.MONITOR, "name": 'x"); os.exit(--'}, 3)
        self.assertIn('output = "x\\"); os.exit(--"', payload)

class TuningTest(unittest.TestCase):
    def setUp(self):
        self.raw = config._default("base")

    def test_switches_flip_choices_cycle_steppers_stop_at_the_ends(self):
        self.assertIs(tuning.next_value(self.raw, "lights.vivid"), False)
        self.assertEqual(tuning.next_value(self.raw, "lights.alert_style"), "blink")
        self.raw["triggers"]["click_strength"] = 8
        self.assertIsNone(tuning.next_value(self.raw, "triggers.click_strength", 1))
        self.assertEqual(tuning.next_value(self.raw, "triggers.click_strength", -1), 7)
        self.raw["sticks"]["scroll_speed"] = 18  # set by hand between two stops
        self.assertEqual(tuning.next_value(self.raw, "sticks.scroll_speed", 1), 20)
        self.assertEqual(tuning.next_value(self.raw, "sticks.scroll_speed", -1), 16)
        self.raw["input"]["stick_deadzone"] = 0.3
        self.assertEqual(tuning.next_value(self.raw, "input.stick_deadzone", wrap=True), 0.08)

    def test_restore_puts_remembered_values_over_the_config_skipping_bad_ones(self):
        saved = {
            "lights.vivid": False, "triggers.click_strength": 3, "sticks.scroll_speed": 25,
            "lights.scanner": "yes", "lights.alert_style": "strobe", "input.stick_deadzone": 0.9,
            "sticks.left": "pointer", "nonsense": 1,
        }
        tuning.restore(self.raw, saved)
        self.assertEqual((self.raw["lights"]["vivid"], self.raw["triggers"]["click_strength"]), (False, 3))
        self.assertEqual(self.raw["sticks"]["scroll_speed"], 25)
        self.assertEqual((self.raw["lights"]["scanner"], self.raw["lights"]["alert_style"]), (True, "alternate"))
        self.assertEqual((self.raw["input"]["stick_deadzone"], self.raw["sticks"]["left"]), (0.18, "buttons"))
        tuning.restore(self.raw, None)  # nothing saved yet, or a broken state file


if __name__ == "__main__":
    unittest.main()
