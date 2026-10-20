"""Tests for restore helpers (pure logic, no compositor)."""

import itertools
import json
import subprocess
import unittest
from unittest import mock

from umbriel_session.config import Config
from umbriel_session.models import Session, WindowEntry, WorkspaceEntry, WorkspaceRef
from umbriel_session.restore import (
    _default_windows,
    _restore_scrolling_layouts,
    _scrolling_column_actions,
    _spawn_cmd,
    _window_actions,
    assign_entries,
    resolve_target_workspace,
    restore_session,
    snapshot_output_areas,
    workspace_selector,
)


def _entry(app_id, title="", index=1, output="HDMI-A-1", named=False, name=""):
    return WindowEntry(
        app_id=app_id, title=title, cmd=app_id, cwd=None, pid=1,
        workspace=WorkspaceRef(
            output=output, index=index, named=named, name=name, id=f"{output}:{index}"
        ),
        output=output, floating=False, x=0, y=0, w=100, h=100,
        fullscreen=False, maximized=False, maximized_to_edges=False,
        pinned=False, focused=False, focus_order=0,
        content_type="none", xdg_tag="", xwayland=False,
        tabbed=False, scratchpad="",
    )


class TestWorkspaceSelector(unittest.TestCase):
    def test_anonymous(self):
        self.assertEqual(
            workspace_selector({"output": "HDMI-A-1", "index": 1, "named": False}),
            "1/HDMI-A-1",
        )

    def test_named(self):
        self.assertEqual(
            workspace_selector(
                {"output": "HDMI-A-1", "index": 2, "named": True, "name": "CHAT"}
            ),
            '"CHAT"/HDMI-A-1',
        )

    def test_named_no_output(self):
        self.assertEqual(
            workspace_selector({"output": "", "index": 2, "named": True, "name": "CHAT"}),
            '"CHAT"',
        )


class TestResolveTargetWorkspace(unittest.TestCase):
    def setUp(self):
        # The serial in the id differs from the index: the index is what
        # Umbriel's own listing reports.
        self.ws_map = {
            "eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1, "named": False, "name": "1"},
            "eDP-1:4": {"id": "eDP-1:4", "output": "eDP-1", "index": 2, "named": False, "name": "2"},
            "eDP-1:6": {"id": "eDP-1:6", "output": "eDP-1", "index": 3, "named": False, "name": "3"},
        }

    def test_resolves_by_index_not_serial(self):
        entry = _entry("zen", index=2, output="eDP-1")
        target = resolve_target_workspace(entry, self.ws_map)
        self.assertEqual(target["id"], "eDP-1:4")
        self.assertEqual(target["index"], 2)

    def test_falls_back_to_last_workspace(self):
        entry = _entry("zen", index=9, output="eDP-1")
        target = resolve_target_workspace(entry, self.ws_map)
        self.assertEqual(target["index"], 3)

    def test_fallback_skips_temporary_workspace(self):
        entry = _entry("zen", index=9, output="eDP-1")
        target = resolve_target_workspace(entry, self.ws_map, "eDP-1:6")
        self.assertEqual(target["index"], 2)

    def test_no_workspace_on_output(self):
        entry = _entry("zen", index=1, output="HDMI-A-1")
        self.assertIsNone(resolve_target_workspace(entry, self.ws_map))

    def test_named_matches_by_name(self):
        ws_map = {
            "eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1, "named": True, "name": "CHAT"},
        }
        entry = _entry("zen", index=4, output="eDP-1", named=True, name="CHAT")
        target = resolve_target_workspace(entry, ws_map)
        self.assertEqual(target["name"], "CHAT")

    def test_named_workspace_wins_over_old_index(self):
        ws_map = {
            "eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1,
                        "named": True, "name": "OTHER"},
            "eDP-1:4": {"id": "eDP-1:4", "output": "eDP-1", "index": 2,
                        "named": True, "name": "CHAT"},
        }
        entry = _entry("zen", index=1, output="eDP-1", named=True, name="CHAT")
        self.assertEqual(resolve_target_workspace(entry, ws_map)["id"], "eDP-1:4")

    def test_missing_named_workspace_does_not_use_another_workspace(self):
        entry = _entry("zen", index=1, output="eDP-1", named=True, name="CHAT")
        self.assertIsNone(resolve_target_workspace(entry, self.ws_map))

    def test_numeric_name_does_not_match_an_unnamed_workspace(self):
        entry = _entry("zen", index=9, output="eDP-1", named=True, name="1")
        self.assertIsNone(resolve_target_workspace(entry, self.ws_map))


class TestFloatingGeometry(unittest.TestCase):
    def setUp(self):
        self.entry = _entry("app", output="eDP-1")
        self.entry.floating = True
        self.entry.x, self.entry.y = -1680, 140
        self.entry.w, self.entry.h = 960, 540
        self.target = {"id": "eDP-1:1", "output": "eDP-1", "index": 1}
        self.areas = {
            "eDP-1": {"x": -1920, "y": 40, "width": 1920, "height": 1080}
        }
        self.current = {
            "workspace": "eDP-1:1", "output": "eDP-1", "floating": True,
            "x": -1800, "y": 60, "w": 640, "h": 480,
        }

    def actions(self, cfg=None):
        return _window_actions(
            self.entry, "w1", self.current, cfg or Config(), self.target, self.areas
        )

    def test_restores_existing_float_size_and_global_position(self):
        self.assertEqual(self.actions(), [
            "window-focus:w1",
            "window-set-primary-extent:0.5000000000",
            "window-set-secondary-extent:0.5000000000",
            "window-move-to:top_left/240/100/w1",
        ])

    def test_tiles_are_floated_before_geometry(self):
        self.current["floating"] = False
        actions = self.actions()
        self.assertEqual(actions[1], "window-toggle-floating:w1")
        self.assertLess(actions.index("window-toggle-floating:w1"),
                        actions.index("window-set-primary-extent:0.5000000000"))
        self.assertFalse(any("center/" in action for action in actions))

    def test_cross_output_move_precedes_geometry_and_refocuses(self):
        self.current.update(workspace="HDMI-A-1:1", output="HDMI-A-1")
        self.assertEqual(self.actions(), [
            "window-focus:w1",
            "window-move-to-workspace-silent:1/eDP-1",
            "window-focus:w1",
            "window-set-primary-extent:0.5000000000",
            "window-set-secondary-extent:0.5000000000",
            "window-move-to:top_left/240/100/w1",
        ])

    def test_already_restored_geometry_is_a_noop(self):
        self.current.update(x=-1680, y=140, w=960, h=540)
        self.assertEqual(self.actions(), [])

    def test_position_only_does_not_take_focus(self):
        self.current.update(w=960, h=540)
        self.assertEqual(self.actions(), ["window-move-to:top_left/240/100/w1"])

    def test_restore_floating_can_be_disabled(self):
        self.current["floating"] = False
        self.assertEqual(self.actions(Config(restore_floating=False)), [])

    def test_tiled_windows_are_not_given_floating_geometry(self):
        self.entry.floating = False
        self.current["floating"] = False
        self.assertEqual(self.actions(), [])

    def test_covering_states_do_not_become_restore_geometry(self):
        for state in ("fullscreen", "maximized", "maximized_to_edges"):
            with self.subTest(state=state):
                setattr(self.entry, state, True)
                self.current[state] = True
                self.assertEqual(self.actions(), [])
                setattr(self.entry, state, False)
                self.current[state] = False

    def test_invalid_saved_size_is_not_sent(self):
        for size in (0, -10):
            with self.subTest(size=size):
                self.entry.w = size
                self.assertEqual(self.actions(), [])

    def test_size_fractions_respect_compositor_limits(self):
        self.entry.w, self.entry.h = 50, 4000
        self.assertEqual(self.actions()[1:3], [
            "window-set-primary-extent:0.1000000000",
            "window-set-secondary-extent:1.0000000000",
        ])

    def test_unavailable_output_area_retains_legacy_float_fallback(self):
        self.current["floating"] = False
        self.areas = {}
        self.assertEqual(self.actions(), [
            "window-toggle-floating:w1", "window-move-to:center/0/0/w1",
        ])

    def test_invalid_output_area_does_not_issue_geometry(self):
        self.areas["eDP-1"]["width"] = 0
        self.assertEqual(self.actions(), [])

    def test_pin_is_applied_after_geometry(self):
        self.entry.pinned = True
        self.assertEqual(self.actions()[-1], "window-toggle-pinned")


class TestOutputAreas(unittest.TestCase):
    def test_reads_global_usable_area_not_physical_resolution(self):
        area = {"x": -1920, "y": 40, "width": 1920, "height": 1040}
        outputs = [
            {"name": "eDP-1", "enabled": True, "usable_area": area},
            {"name": "off", "enabled": False, "usable_area": area},
            {"name": "empty", "usable_area": {"width": 0, "height": 0}},
        ]
        result = subprocess.CompletedProcess([], 0, json.dumps(outputs), "")
        with mock.patch("umbriel_session.restore.run_umbriel", return_value=result) as run:
            self.assertEqual(snapshot_output_areas(), {"eDP-1": area})
        run.assert_called_once_with("outputs", "--json")

    def test_failed_output_query_is_actionable(self):
        result = subprocess.CompletedProcess([], 1, "", "unavailable")
        with (
            mock.patch("umbriel_session.restore.run_umbriel", return_value=result),
            self.assertRaisesRegex(RuntimeError, "outputs failed: unavailable"),
        ):
            snapshot_output_areas()

    def test_malformed_output_query_is_actionable(self):
        result = subprocess.CompletedProcess([], 0, "not json", "")
        with (
            mock.patch("umbriel_session.restore.run_umbriel", return_value=result),
            self.assertRaisesRegex(RuntimeError, "malformed outputs"),
        ):
            snapshot_output_areas()


class TestRestorePlacement(unittest.TestCase):
    def test_adjust_refreshes_workspace_inventory_and_preserves_focus(self):
        entry = _entry("app", index=2, output="eDP-1")
        entry.floating = True
        entry.x, entry.y, entry.w, entry.h = 240, 140, 960, 540
        session = Session(1, 0, "umbriel", [], [entry])
        current = {
            "id": "w1", "app_id": "app", "title": "", "active": True,
            "workspace": "eDP-1:1", "output": "eDP-1", "floating": True,
            "x": 0, "y": 40, "w": 600, "h": 400,
        }
        ws1 = {"id": "eDP-1:1", "output": "eDP-1", "index": 1}
        ws2 = {"id": "eDP-1:4", "output": "eDP-1", "index": 2}
        areas = {"eDP-1": {"x": 0, "y": 40, "width": 1920, "height": 1080}}
        restored = {**current, "workspace": "eDP-1:4", "x": 240, "y": 140, "w": 960, "h": 540}
        with (
            mock.patch("umbriel_session.restore.load_session", return_value=session),
            mock.patch("umbriel_session.restore.snapshot_windows", side_effect=[
                {"w1": current}, {"w1": current}, {"w1": restored},
            ]),
            mock.patch("umbriel_session.restore.snapshot_workspaces", side_effect=[
                {"eDP-1:1": ws1}, {"eDP-1:1": ws1, "eDP-1:4": ws2},
            ]),
            mock.patch("umbriel_session.restore.snapshot_output_areas", return_value=areas),
            mock.patch("umbriel_session.restore._send_many", return_value=True) as send,
            mock.patch("umbriel_session.restore.subprocess.Popen") as spawn,
        ):
            report = restore_session(
                adjust_clients_only=True, cfg=Config(restore_delay_seconds=0)
            )
        self.assertEqual(report, [(entry, "EXISTING")])
        self.assertEqual(send.call_args.args[0], [
            "window-focus:w1",
            "window-move-to-workspace-silent:2/eDP-1",
            "window-focus:w1",
            "window-set-primary-extent:0.5000000000",
            "window-set-secondary-extent:0.5000000000",
            "window-move-to:top_left/240/100/w1",
            "window-focus:w1",
        ])
        spawn.assert_not_called()

    def test_spawned_float_uses_saved_geometry_without_launching_apps(self):
        entry = _entry("app", index=2, output="eDP-1")
        entry.floating = True
        entry.x, entry.y, entry.w, entry.h = 240, 140, 960, 540
        session = Session(1, 0, "umbriel", [], [entry])
        original = {
            "id": "f", "app_id": "other", "title": "Other", "active": True,
            "workspace": "eDP-1:1", "output": "eDP-1",
        }
        spawned = {
            "id": "w1", "app_id": "app", "title": "", "active": False,
            "workspace": "eDP-1:2", "output": "eDP-1", "floating": True,
            "x": 0, "y": 40, "w": 600, "h": 400,
        }
        workspaces = {
            "eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1,
                        "occupied": True},
            "eDP-1:2": {"id": "eDP-1:2", "output": "eDP-1", "index": 2},
        }
        areas = {"eDP-1": {"x": 0, "y": 40, "width": 1920, "height": 1080}}
        subscriber = mock.Mock()
        subscriber.stdout.readline.side_effect = [
            json.dumps({"event": "windows", "data": [original, spawned]}), "",
        ]
        with (
            mock.patch("umbriel_session.restore.load_session", return_value=session),
            mock.patch("umbriel_session.restore.snapshot_windows", side_effect=[
                {"f": original}, {"f": original, "w1": spawned},
                {"f": original, "w1": spawned},
            ]),
            mock.patch("umbriel_session.restore.snapshot_workspaces", return_value=workspaces),
            mock.patch("umbriel_session.restore.snapshot_output_areas", return_value=areas),
            mock.patch("umbriel_session.restore._send_many", return_value=True) as send,
            mock.patch("umbriel_session.restore._which", return_value=True),
            mock.patch("umbriel_session.restore.subprocess.Popen", return_value=subscriber),
            mock.patch("umbriel_session.restore.select.select", return_value=([subscriber.stdout], [], [])),
        ):
            report = restore_session(cfg=Config(restore_delay_seconds=0, spawn_interval=0))
        self.assertEqual(report[0], (entry, "RESTORED"))
        self.assertIn([
            "window-focus:w1",
            "window-set-primary-extent:0.5000000000",
            "window-set-secondary-extent:0.5000000000",
            "window-move-to:top_left/240/100/w1",
            "window-focus:f",
        ], [call.args[0] for call in send.call_args_list])
        subscriber.terminate.assert_called_once()


class TestScrollingColumns(unittest.TestCase):
    def setUp(self):
        # The real saved strip has Zen before both VS Code windows, despite
        # focus_order listing the editors first.
        self.entries = {}
        for wid, title, x, focus_order in (
            ("session", "umbriel-session", 2726, 0),
            ("compositor", "umbriel", 4084, 1),
            ("zen", "Docs", 1368, 3),
        ):
            entry = _entry("zen" if wid == "zen" else "code", title, output="eDP-1")
            entry.x, entry.y, entry.w, entry.h = x, 40, 1346, 718
            entry.maximized, entry.focus_order = True, focus_order
            self.entries[wid] = entry

    def live(self, order):
        return {
            wid: {
                "id": wid, "app_id": self.entries[wid].app_id if wid in self.entries else "extra",
                "title": self.entries[wid].title if wid in self.entries else wid,
                "workspace": "eDP-1:1", "output": "eDP-1", "floating": False,
                "column": column, "row": 0, "tabbed": False,
                "x": 10 + column * 1358, "y": 40, "w": 1346, "h": 718,
                "maximized": True, "active": wid == "session",
            }
            for column, wid in enumerate(order)
        }

    @staticmethod
    def apply_moves(order, actions):
        order = list(order)
        focused = None
        for action in actions:
            if action.startswith("window-focus:"):
                focused = action.split(":", 1)[1]
            elif action == "column-move-to-first":
                order.insert(0, order.pop(order.index(focused)))
            elif action == "column-move-left":
                index = order.index(focused)
                if index > 0:
                    order.insert(index - 1, order.pop(index))
        return order

    def test_saved_x_not_focus_history_restores_real_user_order(self):
        order = ["session", "compositor", "zen"]
        actions = _scrolling_column_actions(self.entries, self.live(order))
        self.assertEqual(actions, ["window-focus:zen", "column-move-to-first"])
        self.assertEqual(self.apply_moves(order, actions), ["zen", "session", "compositor"])
        self.assertFalse(any("floating" in action or "window-move-to:" in action for action in actions))

    def test_all_permutations_preserve_unrelated_column_slots(self):
        for order in itertools.permutations(["zen", "session", "compositor", "extra1", "extra2"]):
            with self.subTest(order=order):
                expected = list(order)
                desired = iter(["zen", "session", "compositor"])
                for index, wid in enumerate(order):
                    if wid in self.entries:
                        expected[index] = next(desired)
                actions = _scrolling_column_actions(self.entries, self.live(order))
                self.assertEqual(self.apply_moves(order, actions), expected)

    def test_already_correct_strip_is_a_noop(self):
        self.assertEqual(_scrolling_column_actions(
            self.entries, self.live(["zen", "session", "compositor"])
        ), [])

    def test_maximized_columns_are_not_resized_from_covering_geometry(self):
        actions = _scrolling_column_actions(
            self.entries, self.live(["session", "compositor", "zen"])
        )
        self.assertFalse(any("extent" in action or "toggle-maximize" in action for action in actions))

    def test_saved_width_uses_viewport_and_gap_not_output_resolution(self):
        live = self.live(["zen", "session", "compositor"])
        self.entries["zen"].maximized = False
        self.entries["zen"].w = 667
        actions = _scrolling_column_actions(self.entries, live)
        # (667 + 12) / (1346 + 12) = 0.5, not 667 / 1366.
        self.assertEqual(actions, ["window-focus:zen", "window-set-primary-extent:0.5000000000"])

    def test_width_without_known_viewport_is_not_guessed(self):
        live = self.live(["zen", "session", "compositor"])
        for window in live.values():
            window["maximized"] = False
        for entry in self.entries.values():
            entry.maximized = False
        self.entries["zen"].w = 667
        self.assertEqual(_scrolling_column_actions(self.entries, live), [])

    def test_full_width_is_restored_after_layout_conversion(self):
        live = self.live(["zen", "session", "compositor"])
        actions = _scrolling_column_actions(self.entries, live, reset_maximized=True)
        self.assertEqual(actions, [
            "window-focus:session", "window-set-primary-extent:1.0000000000", "window-toggle-maximize",
            "window-focus:compositor", "window-set-primary-extent:1.0000000000", "window-toggle-maximize",
            "window-focus:zen", "window-set-primary-extent:1.0000000000", "window-toggle-maximize",
        ])

    def test_nonmaximized_conversion_width_is_not_inferred_from_stale_flags(self):
        live = self.live(["zen", "session", "compositor"])
        self.entries["zen"].maximized = False
        self.entries["zen"].w = 667
        actions = _scrolling_column_actions(self.entries, live, reset_maximized=True)
        self.assertFalse(any(action == "window-focus:zen" for action in actions))
        self.assertNotIn("window-set-primary-extent:0.5000000000", actions)

    def test_ambiguous_saved_stacks_and_tabs_are_skipped(self):
        live = self.live(["session", "compositor", "zen"])
        for field, value in (("x", 2726), ("tabbed", True), ("y", 400), ("fullscreen", True)):
            with self.subTest(field=field):
                original = getattr(self.entries["zen"], field)
                setattr(self.entries["zen"], field, value)
                self.assertEqual(_scrolling_column_actions(self.entries, live), [])
                setattr(self.entries["zen"], field, original)

    def test_live_shared_columns_are_not_split_or_moved(self):
        live = self.live(["session", "compositor", "zen"])
        live["compositor"].update(column=0, row=1)
        live["zen"]["column"] = 1
        self.assertEqual(_scrolling_column_actions(self.entries, live), [])

    def test_vertical_live_layout_is_not_given_horizontal_moves(self):
        live = self.live(["session", "compositor", "zen"])
        for index, window in enumerate(live.values()):
            window.update(x=10, y=40 + 730 * index)
        self.assertEqual(_scrolling_column_actions(self.entries, live), [])

    def test_missing_column_metadata_is_not_guessed_from_focus(self):
        live = self.live(["session", "compositor", "zen"])
        del live["zen"]["column"]
        self.assertEqual(_scrolling_column_actions(self.entries, live), [])

    def session(self):
        workspace = WorkspaceEntry("eDP-1:1", "1", False, 1, "eDP-1", "scrolling", False)
        return Session(1, 0, "umbriel", [workspace], list(self.entries.values()))

    def test_layout_conversion_refreshes_metadata_before_reordering(self):
        live = self.live(["session", "compositor", "zen"])
        target = {"id": "eDP-1:1", "index": 1, "output": "eDP-1", "layout": "dwindle"}
        original = {"id": "eDP-1:2", "index": 2, "output": "eDP-1"}
        matched = {wid: (entry, False) for wid, entry in self.entries.items()}
        with (
            mock.patch("umbriel_session.restore._send_many", return_value=True) as send,
            mock.patch("umbriel_session.restore.snapshot_windows", return_value=live) as snapshot,
        ):
            _restore_scrolling_layouts(
                self.session(), matched, live, {target["id"]: target}, original, "f", None
            )
        self.assertEqual(send.call_args_list[0].args[0], [
            "workspace-switch:1/eDP-1", "workspace-set-layout:scrolling",
            "workspace-switch:2/eDP-1", "window-focus:f",
        ])
        placement = send.call_args_list[1].args[0]
        self.assertEqual(placement[:3], [
            "workspace-switch:1/eDP-1", "window-focus:zen", "column-move-to-first",
        ])
        self.assertEqual(placement[-2:], ["workspace-switch:2/eDP-1", "window-focus:f"])
        self.assertEqual(snapshot.call_count, 2)

    def test_failed_layout_conversion_does_not_reorder_old_layout(self):
        live = self.live(["session", "compositor", "zen"])
        target = {"id": "eDP-1:1", "index": 1, "output": "eDP-1", "layout": "master"}
        matched = {wid: (entry, False) for wid, entry in self.entries.items()}
        with (
            mock.patch("umbriel_session.restore._send_many", return_value=False) as send,
            mock.patch("umbriel_session.restore.snapshot_windows") as snapshot,
        ):
            _restore_scrolling_layouts(
                self.session(), matched, live, {target["id"]: target}, target, "session", None
            )
        self.assertEqual(send.call_count, 1)
        snapshot.assert_not_called()

    def test_restore_session_runs_final_column_pass_for_existing_windows(self):
        live = self.live(["session", "compositor", "zen"])
        live["session"]["active"] = False
        session = self.session()
        second = _entry("zen", "Phoronix", index=2, output="eDP-1")
        second.x, second.y, second.w, second.h = 10, 40, 1346, 718
        second.maximized, second.focus_order = True, 2
        session.windows.append(second)
        session.workspaces.append(WorkspaceEntry(
            "eDP-1:2", "2", False, 2, "eDP-1", "scrolling", False
        ))
        live["zen2"] = {
            "id": "zen2", "app_id": "zen", "title": "Phoronix", "active": True,
            "workspace": "eDP-1:2", "output": "eDP-1", "floating": False,
            "column": 0, "row": 0, "x": 10, "y": 40, "w": 1346, "h": 718,
            "maximized": True,
        }
        target = {"id": "eDP-1:1", "index": 1, "output": "eDP-1", "layout": "scrolling"}
        original = {"id": "eDP-1:2", "index": 2, "output": "eDP-1", "layout": "scrolling"}
        with (
            mock.patch("umbriel_session.restore.load_session", return_value=session),
            mock.patch("umbriel_session.restore.snapshot_windows", return_value=live),
            mock.patch("umbriel_session.restore.snapshot_workspaces", return_value={
                target["id"]: target, original["id"]: original,
            }),
            mock.patch("umbriel_session.restore._send_many", return_value=True) as send,
            mock.patch("umbriel_session.restore.subprocess.Popen") as spawn,
        ):
            report = restore_session(adjust_clients_only=True, cfg=Config(restore_delay_seconds=0))
        self.assertTrue(all(status == "EXISTING" for _entry, status in report))
        send.assert_called_once_with([
            "workspace-switch:1/eDP-1", "window-focus:zen", "column-move-to-first",
            "workspace-switch:2/eDP-1", "window-focus:zen2",
        ])
        self.assertEqual(len(report), 4)
        spawn.assert_not_called()

    def test_numeric_fallback_does_not_merge_saved_strips(self):
        live = self.live(["session", "compositor", "zen"])
        target = {"id": "eDP-1:1", "index": 3, "output": "eDP-1", "layout": "scrolling"}
        matched = {wid: (entry, False) for wid, entry in self.entries.items()}
        with mock.patch("umbriel_session.restore._send_many") as send:
            _restore_scrolling_layouts(
                self.session(), matched, live, {target["id"]: target}, target, "session", None
            )
        send.assert_not_called()


class TestAssignEntries(unittest.TestCase):
    def setUp(self):
        self.ws_map = {
            "eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1, "named": False, "name": "1"},
            "eDP-1:2": {"id": "eDP-1:2", "output": "eDP-1", "index": 2, "named": False, "name": "2"},
        }

    def _assign(self, live, entries):
        pending = [(e, e.cmd) for e in entries]
        return assign_entries(live, pending, self.ws_map)

    def test_match_by_title(self):
        entries = [_entry("zen", "Zen A"), _entry("zen", "Zen B")]
        live = {"w1": {"app_id": "zen", "title": "Zen B", "workspace": "eDP-1:1"}}
        assignment = self._assign(live, entries)
        self.assertEqual(assignment["w1"].title, "Zen B")

    def test_match_by_workspace_when_title_shared(self):
        entries = [
            _entry("zen", "Zen", index=1, output="eDP-1"),
            _entry("zen", "Zen", index=2, output="eDP-1"),
        ]
        live = {"w1": {"app_id": "zen", "title": "Zen", "workspace": "eDP-1:2"}}
        assignment = self._assign(live, entries)
        self.assertEqual(assignment["w1"].workspace.index, 2)

    def test_title_wins_over_workspace(self):
        # The window on workspace 2 owns the entry titled after it, even
        # though the other window sits on the entry's workspace.
        entries = [
            _entry("code", "kaenime", index=1, output="eDP-1"),
            _entry("code", "other", index=2, output="eDP-1"),
        ]
        live = {
            "w1": {"app_id": "code", "title": "other", "workspace": "eDP-1:2"},
            "w2": {"app_id": "code", "title": "kaenime", "workspace": "eDP-1:1"},
        }
        assignment = self._assign(live, entries)
        self.assertEqual(assignment["w1"].title, "other")
        self.assertEqual(assignment["w2"].title, "kaenime")

    def test_window_without_unique_title_does_not_steal(self):
        # An extra window of an app must not take an entry that a real
        # window of that app still needs.
        entries = [
            _entry("code", "kaenime", index=1, output="eDP-1"),
            _entry("code", "umbriel-session", index=1, output="eDP-1"),
        ]
        live = {
            "w1": {"app_id": "code", "title": "Visual Studio Code", "workspace": "eDP-1:1"},
            "w2": {"app_id": "code", "title": "kaenime", "workspace": "eDP-1:2"},
            "w3": {"app_id": "code", "title": "umbriel-session", "workspace": "eDP-1:1"},
        }
        assignment = self._assign(live, entries)
        self.assertNotIn("w1", assignment)
        self.assertEqual(assignment["w2"].title, "kaenime")
        self.assertEqual(assignment["w3"].title, "umbriel-session")

    def test_moved_window_is_recognised_by_app(self):
        # A window whose title changed but that is the only one of its app
        # still belongs to the saved entry.
        entries = [_entry("zen", "CachyOS", index=1, output="eDP-1")]
        live = {"w1": {"app_id": "zen", "title": "Zen Browser", "workspace": "eDP-1:2"}}
        assignment = self._assign(live, entries)
        self.assertEqual(assignment["w1"].title, "CachyOS")

    def test_extra_window_of_same_app_is_unmatched(self):
        entries = [_entry("zen", "Zen", index=1, output="eDP-1")]
        live = {
            "w1": {"app_id": "zen", "title": "Zen", "workspace": "eDP-1:1"},
            "w2": {"app_id": "zen", "title": "Zen", "workspace": "eDP-1:1"},
        }
        assignment = self._assign(live, entries)
        self.assertEqual(len(assignment), 1)

    def test_no_match(self):
        entries = [_entry("zen", "Zen A")]
        live = {"w1": {"app_id": "firefox", "title": "other", "workspace": "eDP-1:1"}}
        self.assertEqual(self._assign(live, entries), {})


class TestDefaultWindows(unittest.TestCase):
    def _live(self):
        return {
            "w1": {
                "app_id": "code",
                "title": "kaenime-api-go - Visual Studio Code",
                "workspace": "eDP-1:1",
            },
            "w2": {
                "app_id": "code",
                "title": "Visual Studio Code",
                "workspace": "eDP-1:1",
            },
        }

    def test_blank_window_is_default(self):
        defaults = _default_windows(self._live())
        self.assertEqual(defaults, {"w2"})

    def test_identical_titles_are_not_default(self):
        live = {
            "w1": {"app_id": "zen", "title": "Zen Browser", "workspace": "eDP-1:1"},
            "w2": {"app_id": "zen", "title": "Zen Browser", "workspace": "eDP-1:1"},
        }
        self.assertEqual(_default_windows(live), set())

    def test_default_window_does_not_take_titled_entry(self):
        # The blank window of an Electron app must not take the entry of a
        # titled window that is no longer running.
        entries = [
            _entry("code", "kaenime-api-go - Visual Studio Code", index=1, output="eDP-1"),
            _entry("code", "umbriel-session - Visual Studio Code", index=1, output="eDP-1"),
        ]
        live = {
            "w1": {
                "app_id": "code",
                "title": "kaenime-api-go - Visual Studio Code",
                "workspace": "eDP-1:1",
            },
            "w2": {
                "app_id": "code",
                "title": "Visual Studio Code",
                "workspace": "eDP-1:1",
            },
        }
        pending = [(e, e.cmd) for e in entries]
        ws_map = {"eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1}}
        assignment = assign_entries(live, pending, ws_map)
        self.assertEqual(assignment["w1"].title, "kaenime-api-go - Visual Studio Code")
        self.assertNotIn("w2", assignment)

    def test_default_window_takes_default_entry(self):
        entries = [_entry("code", "Visual Studio Code", index=1, output="eDP-1")]
        live = {
            "w1": {"app_id": "code", "title": "Visual Studio Code", "workspace": "eDP-1:1"},
        }
        pending = [(e, e.cmd) for e in entries]
        ws_map = {"eDP-1:1": {"id": "eDP-1:1", "output": "eDP-1", "index": 1}}
        assignment = assign_entries(live, pending, ws_map)
        self.assertEqual(assignment["w1"].title, "Visual Studio Code")


class TestSpawnCmd(unittest.TestCase):
    def test_launch_by_title_wins(self):
        cfg = Config()
        cfg.launch_by_title = {"umbriel-session - Visual Studio Code": "code /path"}
        entry = _entry("code", "umbriel-session - Visual Studio Code")
        self.assertEqual(_spawn_cmd(entry, cfg), "code /path")

    def test_falls_back_to_entry_cmd(self):
        cfg = Config()
        entry = _entry("code", "whatever")
        entry.cmd = "code"
        self.assertEqual(_spawn_cmd(entry, cfg), "code")


if __name__ == "__main__":
    unittest.main()
