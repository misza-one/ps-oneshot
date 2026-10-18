import json
import os
import shlex
import tempfile
import time
import unittest
from dataclasses import replace
from unittest import mock

from gi.repository import GLib

from ps_oneshot import config, lights, sounds
from ps_oneshot.apps import DesktopApp, app_for_window, window_of
from ps_oneshot.config import MenuItem
from ps_oneshot.dictation import DictationLog
from ps_oneshot.favorites import Favorites, place, shift
from ps_oneshot.herd import Agent, agent_in_window, by_machine, herdr_command, session_windows
from ps_oneshot.labels import cheatsheet_rows, pair_text, radial_rows
from ps_oneshot.menus import LOCAL_COLOR, machine_groups
from ps_oneshot.couch import put_back_entries, take_entries
from ps_oneshot.pad import stick_direction
from ps_oneshot.sources import LevelControl, audio_devices, next_stop
from ps_oneshot.terminals import foot_font


def agent(key, host, hostname="", tab=""):
    return Agent(key=key, project="p", kind="omp", status="idle", title="π ⠏ Fix the tests", pane_id="x",
                 session="default", host=host, hostname=hostname, since=0, tab=tab)


class StickDirectionTest(unittest.TestCase):
    def test_threshold_and_dominant_axis(self):
        self.assertIsNone(stick_direction(0.5, 0.0, 0.6, None))
        self.assertEqual(stick_direction(0.7, 0.2, 0.6, None), "right")
        self.assertEqual(stick_direction(-0.1, -0.9, 0.6, None), "up")

    def test_hysteresis_keeps_direction_until_well_below_threshold(self):
        self.assertEqual(stick_direction(0.45, 0.0, 0.6, "right"), "right")  # above 0.6 * 0.65
        self.assertIsNone(stick_direction(0.3, 0.0, 0.6, "right"))
        # A wobble towards another axis below the threshold does not switch direction.
        self.assertEqual(stick_direction(0.3, 0.4, 0.6, "right"), "right")
        self.assertEqual(stick_direction(0.1, 0.9, 0.6, "right"), "down")


class DictationLogTest(unittest.TestCase):
    def test_back_pops_newest_phrase_per_window_then_reports_empty(self):
        log = DictationLog(suffix=" ")
        log.add("A", "hello")
        log.add("A", "zażółć")
        log.add("B", "other")
        self.assertEqual(log.pop_last("A"), len("zażółć "))
        self.assertEqual(log.pop_last("A"), len("hello "))
        self.assertIsNone(log.pop_last("A"))
        self.assertEqual(log.pop_last("B"), len("other "))

    def test_clear_after_enter_and_pop_all(self):
        log = DictationLog()
        log.add("A", "one")
        log.add("A", "three")
        self.assertEqual(log.pop_all("A"), 8)
        self.assertIsNone(log.pop_all("A"))
        log.add("A", "sent")
        log.clear("A")
        self.assertIsNone(log.pop_last("A"))


class TextStopTest(unittest.TestCase):
    STOPS = [9, 10, 11, 12, 14, 16, 20]

    def test_steps_from_on_and_between_stops_and_stops_at_the_ends(self):
        self.assertEqual(next_stop(self.STOPS, 11, 1), 12)
        self.assertEqual(next_stop(self.STOPS, 13, 1), 14)  # a size set by hand, between notches
        self.assertEqual(next_stop(self.STOPS, 13, -1), 12)
        self.assertEqual(next_stop([0.9, 1.0, 1.1], 0.9091, 1), 1.0)  # GTK factor read back as a float
        self.assertIsNone(next_stop(self.STOPS, 20, 1))
        self.assertIsNone(next_stop(self.STOPS, 9, -1))


class LevelControlTest(unittest.TestCase):
    def test_steps_during_a_slow_set_coalesce_into_one_more_set_to_the_final_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            sets = os.path.join(tmp, "sets")
            control = LevelControl({"get": "echo 40", "set": f"sleep 0.1; echo {{value}} >> {sets}", "step": 5})
            targets = [control.step(1) for _ in range(6)]  # a held button: six steps while the first set runs
            self.assertEqual(targets, [45, 50, 55, 60, 65, 70])
            context = GLib.MainContext.default()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not (control._target is None and not control._busy):
                context.iteration(False)
                time.sleep(0.01)
            with open(sets) as handle:
                self.assertEqual(handle.read().split(), ["45", "70"])
            self.assertEqual(control.step(-1), 65)  # continues from what was set, no re-read


class AudioDevicesTest(unittest.TestCase):
    def sink(self, name, availability=("available",), monitor=False):
        return {"name": name, "ports": [{"availability": a} for a in availability],
                "properties": {"device.class": "monitor" if monitor else "sound"}}

    def test_drops_monitors_and_unplugged_ports_and_marks_defaults(self):
        sinks = [self.sink("spk"), self.sink("hp", ("not available",)), self.sink("virt", ())]
        sources = [self.sink("spk.monitor", monitor=True), self.sink("mic"), self.sink("line", ("not available",))]
        devices = audio_devices(sinks, sources, "virt", "mic")
        self.assertEqual([(d["name"], d["role"], d["active"]) for d in devices],
                         [("spk", "output", False), ("virt", "output", True), ("mic", "input", True)])

    def test_caps_at_twelve_and_keeps_the_active_device(self):
        sinks = [self.sink(f"out{i}") for i in range(10)]
        sources = [self.sink(f"in{i}") for i in range(10)]
        devices = audio_devices(sinks, sources, "out9", "in0")
        self.assertEqual(len(devices), 12)
        names = [d["name"] for d in devices]
        self.assertIn("out9", names)
        self.assertIn("in0", names)


class MachineGroupingTest(unittest.TestCase):
    def test_local_first_then_hosts_in_order_of_appearance(self):
        agents = [agent("a", "main", "build-srv"), agent("b", "local", "ws"), agent("c", "laptop"), agent("d", "main")]
        self.assertEqual([a.key for a in by_machine(agents)], ["b", "a", "d", "c"])
        groups = machine_groups(by_machine(agents))
        self.assertEqual(list(groups), ["local", "main", "laptop"])
        self.assertTrue(groups["local"][0].endswith("ws"))
        self.assertEqual(groups["local"][1], LOCAL_COLOR)
        self.assertTrue(groups["laptop"][0].endswith("laptop"))  # no hostname: SSH alias

    def test_caption_prefers_named_tab_over_title(self):
        self.assertEqual(agent("a", "local", tab="landing page").caption, "landing page")
        self.assertEqual(agent("a", "local", tab="1").caption, "Fix the tests")

    def test_window_title_maps_to_the_focused_agent_of_that_workspace(self):
        base = agent("x", "main", "build-srv")
        agents = [
            replace(base, key="shop-1", project="shop", kind="claude"),
            replace(base, key="shop-2", project="shop", kind="omp", focused=True),
            replace(base, key="other", project="web", kind="codex", focused=True),
            replace(agent("l", "local", "ws"), project="shop", kind="pi", focused=True),
        ]
        self.assertEqual(agent_in_window(agents, "build-srv: shop").key, "shop-2")
        self.assertEqual(agent_in_window(agents, "main: web").key, "other")  # SSH alias works too
        self.assertIsNone(agent_in_window(agents, "build-srv: nope"))
        self.assertIsNone(agent_in_window(agents, "Firefox"))

    def test_session_windows_are_the_terminals_holding_a_client_latest_first(self):
        remote = "exec /bin/sh -lc 'PATH=x; export PATH; exec herdr --session {}'"
        attach = ["herdr", "--session", "default", "agent", "attach", "w1:p1"]
        processes = {  # pid: (parent, comm, argv)
            100: (1, "foot", ["foot"]), 101: (100, "herdr", ["herdr", "--session", "default"]),
            200: (1, "foot", ["foot"]), 201: (200, "ssh", ["ssh", "-t", "--", "main", remote.format("default")]),
            300: (1, "foot", ["foot"]), 301: (300, "herdr", attach),
            400: (1, "my term)", ["foot"]), 401: (400, "herdr", ["herdr"]),
            500: (1, "foot", ["foot"]),
            501: (500, "ssh", ["ssh", "-t", "--", "main", remote.format("default agent attach w4:p1")]),
            600: (1, "foot", ["foot"]), 601: (600, "ssh", ["ssh", "-t", "--", "main", remote.format("default2")]),
        }
        clients = [{"pid": pid, "address": f"0x{pid}", "focusHistoryID": rank}
                   for pid, rank in ((100, 3), (200, 1), (300, 2), (400, 0), (500, 4), (600, 5))]
        with tempfile.TemporaryDirectory() as proc:
            for pid, (parent, comm, argv) in processes.items():
                os.mkdir(os.path.join(proc, str(pid)))
                with open(os.path.join(proc, str(pid), "cmdline"), "wb") as handle:
                    handle.write(b"\0".join(part.encode() for part in argv) + b"\0")
                with open(os.path.join(proc, str(pid), "stat"), "w") as handle:
                    handle.write(f"{pid} ({comm}) S {parent} 0 0\n")
            # One-agent `agent attach` windows (local or over ssh) aren't clients of the session.
            self.assertEqual(session_windows("local", "default", clients, proc), ["0x400", "0x100"])
            self.assertEqual(session_windows("main", "default", clients, proc), ["0x200"])
            self.assertEqual(session_windows("laptop", "default", clients, proc), [])

    def test_herdr_runs_here_or_reaches_the_remote_shell_intact(self):
        self.assertEqual(herdr_command("local", "default", "pane", "close", "w1:p1"),
                         ["herdr", "--session", "default", "pane", "close", "w1:p1"])
        command = herdr_command("main", "my session", "pane", "close", "w4:p10")
        self.assertEqual(command[0], "ssh")
        self.assertEqual(command[command.index("--") + 1], "main")
        _exec, shell, flag, script = shlex.split(command[-1])  # what the remote login shell receives
        self.assertEqual((shell, flag), ("/bin/sh", "-lc"))
        self.assertEqual(shlex.split(script.split("; exec ", 1)[1]),
                         ["herdr", "--session", "my session", "pane", "close", "w4:p10"])


class CheatsheetRowsTest(unittest.TestCase):
    def test_directional_groups_collapse(self):
        raw = config._default("base")
        cfg = config.build(raw, "hyprland", [])
        rows = dict(cheatsheet_rows(cfg.layers["normal"], {}))
        self.assertEqual(rows["L-stick"], "Arrow keys")
        self.assertEqual(rows["D-pad"], "Focus window")
        self.assertNotIn("D-pad ↑", rows)
        keys = {f"dpad_{d}": f"key:ctrl+{k}" for d, k in zip(("up", "down", "left", "right"), "cvxz")}
        custom = config.build(config.deep_merge(raw, {"bindings": {"normal": keys}}), "hyprland", [])
        rows = dict(cheatsheet_rows(custom.layers["normal"], {}))
        self.assertEqual(rows["D-pad ↑ ↓"], "Ctrl + C / V")  # four unrelated actions stay apart, not "Ctrl +"

    def test_opposite_pairs_say_shared_words_once(self):
        self.assertEqual(pair_text("Volume up", "Volume down"), "Volume up / down")
        self.assertEqual(
            pair_text("Text bigger (focused window)", "Text smaller (focused window)"),
            "Text bigger / smaller (focused window)",
        )
        self.assertEqual(pair_text("Bigger", "Biggest"), "Bigger / Biggest")  # never splits a word


class FootFontTest(unittest.TestCase):
    def test_size_and_zoom_step_from_main_only(self):
        self.assertEqual(foot_font("font=JetBrainsMono Nerd Font:size=11\n"), (11.0, 0.5))  # foot's default step
        ini = "[main]\nfont=Mono:size=9\nfont-size-adjustment=1pt\n[colors]\nfont=Other:size=99\n"
        self.assertEqual(foot_font(ini), (9.0, 1.0))
        self.assertEqual(foot_font("font=Mono:size=9\nfont-size-adjustment=10%\n")[1], 0.0)  # can't map to pt
        self.assertEqual(foot_font("font=Mono\n")[0], None)


class CouchBarTest(unittest.TestCase):
    def test_taken_widgets_go_back_to_their_places_duplicates_included(self):
        layout = {
            "left": [{"id": "omarchy.menu"}, {"id": "omarchy.weather"}, {"id": "x.radio", "station": 3}],
            "right": [{"id": "omarchy.tray"}, "omarchy.indicators", {"id": "omarchy.audio"}, "omarchy.indicators"],
        }
        original = json.loads(json.dumps(layout))
        taken = take_entries(layout, {"omarchy.weather", "x.radio", "omarchy.indicators"})
        self.assertEqual(layout, {"left": [{"id": "omarchy.menu"}], "right": [{"id": "omarchy.tray"}, {"id": "omarchy.audio"}]})
        put_back_entries(layout, json.loads(json.dumps(taken)))  # as stored in state.json
        self.assertEqual(layout, original)


class AlertBlinkTest(unittest.TestCase):
    def test_agent_alert_blinks_hard_on_and_off_but_recording_wins(self):
        cfg = config._default("base")["lights"]
        engine = lights.LightEngine(cfg, (1, 2, 3))
        waiting = lights.LightInputs(herd_status="blocked")
        period = cfg["blink_period"]
        self.assertEqual(engine.frame(waiting, period * 0.1).brightness, cfg["brightness"])
        self.assertEqual(engine.frame(waiting, period * 0.6).brightness, 0.0)
        recording = engine.frame(lights.LightInputs(herd_status="blocked", recording=True), period * 0.6)
        self.assertEqual(list(recording.rgb), cfg["recording"])
        self.assertGreater(recording.brightness, 0.0)

    def test_alert_alternates_with_the_waiting_agents_colour(self):
        cfg = config._default("base")["lights"]
        engine = lights.LightEngine(cfg, (1, 2, 3))
        claude = (224, 145, 90)
        waiting = lights.LightInputs(herd_status="blocked", alert_rgb=claude)
        period = cfg["blink_period"]
        first, second = engine.frame(waiting, period * 0.1), engine.frame(waiting, period * 0.6)
        self.assertEqual(list(first.rgb), cfg["blocked"])
        self.assertEqual((second.rgb, second.brightness), (lights.vivid(claude), cfg["brightness"]))
        # An agent coloured like its alert (Kimi's yellow, done) would look solid: dark instead.
        kimi = lights.LightInputs(herd_status="done", alert_rgb=(253, 224, 71))
        self.assertEqual(engine.frame(kimi, period * 0.6).brightness, 0.0)
        blink = lights.LightEngine({**cfg, "alert_style": "blink"}, (1, 2, 3))
        self.assertEqual(blink.frame(waiting, period * 0.6).brightness, 0.0)


class RadialRowsTest(unittest.TestCase):
    def test_plain_items_share_a_row_and_submenus_get_their_own(self):
        items = (
            MenuItem(label="Terminal", action="exec:t"),
            MenuItem(label="Audio", items=(MenuItem(label="Mute", action="exec:m"),)),
            MenuItem(label="Dictation", source="voxtype-language"),
            MenuItem(label="Lock", action="exec:l"),
        )
        rows = radial_rows(items)
        self.assertEqual(rows[0], ("Items", "Terminal · Lock"))
        self.assertEqual([key for key, _ in rows[1:]], ["Audio ›", "Dictation ›"])


class DesktopAppTest(unittest.TestCase):
    whatsapp = DesktopApp(id="WhatsApp.desktop", name="WhatsApp", url="https://web.whatsapp.com/")
    nautilus = DesktopApp(id="org.gnome.Nautilus.desktop", name="Files")
    chrome = DesktopApp(id="google-chrome.desktop", name="Google Chrome", wm_class="google-chrome")

    def test_windows_match_by_wm_class_desktop_id_or_web_app_host(self):
        apps = [self.chrome, self.whatsapp, self.nautilus]
        self.assertIs(app_for_window("chrome-web.whatsapp.com__-Default", apps), self.whatsapp)
        self.assertIs(app_for_window("google-chrome", apps), self.chrome)
        self.assertIs(app_for_window("org.gnome.nautilus", apps), self.nautilus)  # app ids ignore case
        self.assertIsNone(app_for_window("chrome-whatsapp.com__-Default", apps))  # another host
        self.assertIsNone(app_for_window("", apps))

    def test_focuses_the_most_recently_used_window(self):
        clients = [
            {"class": "org.gnome.Nautilus", "address": "0xa", "focusHistoryID": 3},
            {"class": "foot", "address": "0xb", "focusHistoryID": 0},
            {"class": "org.gnome.Nautilus", "address": "0xc", "focusHistoryID": 1},
        ]
        self.assertEqual(window_of(self.nautilus, clients), "0xc")
        self.assertIsNone(window_of(self.whatsapp, clients))


class FavoritesTest(unittest.TestCase):
    def test_placing_an_app_that_is_already_pinned_swaps_the_two_slots(self):
        a, b, c = ({"action": f"exec:{name}"} for name in "abc")
        slots = [a, b, None]
        place(slots, 0, b)
        self.assertEqual(slots, [b, a, None])
        place(slots, 2, c)
        self.assertEqual(slots, [b, a, c])

    def test_moving_stops_at_either_end(self):
        slots = [{"action": "exec:a"}, None]
        self.assertEqual(shift(slots, 0, 1), 1)
        self.assertEqual(slots, [None, {"action": "exec:a"}])
        self.assertIsNone(shift(slots, 1, 1))
        self.assertIsNone(shift(slots, 0, -1))

    def test_saved_slots_win_over_the_seeds_and_fit_the_slot_count(self):
        cfg = {"slots": 3, "items": [{"label": "Seed", "action": "exec:seed"}]}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(sounds, "STATE_FILE", os.path.join(directory, "state.json")):
            self.assertEqual(Favorites(cfg).slots, [{"label": "Seed", "action": "exec:seed"}, None, None])
            sounds.save_state("favorites", [None, {"action": "launch:foot.desktop"}, "junk", {"action": "exec:x"}])
            self.assertEqual(Favorites(cfg).slots, [None, {"action": "launch:foot.desktop"}, None])


if __name__ == "__main__":
    unittest.main()
