"""Focus-preserving restore behavior and batched IPC."""

import json
import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

from umbriel_session.config import Config
from umbriel_session.models import Session, WindowEntry, WorkspaceRef
from umbriel_session.restore import (
    _send_many,
    _spawn_batch_actions,
    _temp_workspace,
    _window_actions,
    restore_session,
)


def _entry(app_id, title="", index=1, output="eDP-1", focus_order=0):
    return WindowEntry(
        app_id=app_id,
        title=title,
        cmd=app_id,
        cwd=None,
        pid=1,
        workspace=WorkspaceRef(
            output=output,
            index=index,
            named=False,
            name=str(index),
            id=f"{output}:{index}",
        ),
        output=output,
        floating=False,
        x=0,
        y=0,
        w=100,
        h=100,
        fullscreen=False,
        maximized=False,
        maximized_to_edges=False,
        pinned=False,
        focused=False,
        focus_order=focus_order,
        content_type="none",
        xdg_tag="",
        xwayland=False,
        tabbed=False,
        scratchpad="",
    )


def _live(
    wid,
    app_id,
    title,
    workspace,
    *,
    output="eDP-1",
    active=False,
    floating=False,
    fullscreen=False,
    maximized=False,
    pinned=False,
):
    return {
        "id": wid,
        "app_id": app_id,
        "title": title,
        "workspace": workspace,
        "output": output,
        "active": active,
        "floating": floating,
        "fullscreen": fullscreen,
        "maximized": maximized,
        "pinned": pinned,
    }


WS_MAP = {
    "eDP-1:1": {
        "id": "eDP-1:1",
        "output": "eDP-1",
        "index": 1,
        "named": False,
        "name": "1",
        "focused": True,
        "occupied": True,
        "window_count": 1,
    },
    "eDP-1:2": {
        "id": "eDP-1:2",
        "output": "eDP-1",
        "index": 2,
        "named": False,
        "name": "2",
        "focused": False,
        "occupied": True,
        "window_count": 1,
    },
}


class TestWindowActions(unittest.TestCase):
    def test_state_actions_focus_before_moving(self):
        entry = _entry("app", index=1)
        entry.fullscreen = True
        entry.maximized = True
        entry.pinned = True
        current = _live("w1", "app", "App", "eDP-1:1")
        actions = _window_actions(entry, "w1", current, Config(), WS_MAP["eDP-1:2"])
        self.assertEqual(
            actions,
            [
                "window-focus:w1",
                "window-toggle-fullscreen",
                "window-toggle-maximize",
                "window-toggle-pinned",
                "window-move-to-workspace-silent:2/eDP-1",
            ],
        )

    def test_window_already_placed_needs_no_focus(self):
        entry = _entry("app", index=2)
        current = _live("w1", "app", "App", "eDP-1:2")
        self.assertEqual(
            _window_actions(entry, "w1", current, Config(), WS_MAP["eDP-1:2"]),
            [],
        )

    def test_floating_move_targets_the_window_id(self):
        entry = _entry("app", index=1)
        entry.floating = True
        current = _live("w1", "app", "App", "eDP-1:1")
        actions = _window_actions(entry, "w1", current, Config(), WS_MAP["eDP-1:1"])
        self.assertEqual(
            actions,
            ["window-toggle-floating:w1", "window-move-to:center/0/0/w1"],
        )


class TestTempWorkspace(unittest.TestCase):
    def test_after_last_workspace_on_the_output(self):
        temp = _temp_workspace(WS_MAP, "eDP-1:1")
        self.assertIsNotNone(temp)
        self.assertEqual(temp["output"], "eDP-1")
        self.assertEqual(temp["index"], 3)

    def test_reuses_an_empty_workspace(self):
        ws_map = dict(WS_MAP)
        ws_map["eDP-1:3"] = {
            "id": "eDP-1:3",
            "output": "eDP-1",
            "index": 3,
            "named": False,
            "name": "3",
            "occupied": False,
            "window_count": 0,
        }
        self.assertEqual(_temp_workspace(ws_map, "eDP-1:1")["index"], 3)

    def test_does_not_reuse_a_reserved_workspace(self):
        ws_map = dict(WS_MAP)
        ws_map["eDP-1:3"] = {
            "id": "eDP-1:3",
            "output": "eDP-1",
            "index": 3,
            "named": False,
            "name": "3",
            "occupied": False,
            "window_count": 0,
        }
        temp = _temp_workspace(
            ws_map, "eDP-1:1", {("eDP-1", 3)}
        )
        self.assertEqual(temp["index"], 4)

    def test_missing_original(self):
        self.assertIsNone(_temp_workspace(WS_MAP, None))


class TestSpawnBatch(unittest.TestCase):
    def test_switches_back_and_restores_focus(self):
        actions = _spawn_batch_actions(
            "kitty",
            {"output": "eDP-1", "index": 3, "named": False, "name": "3"},
            WS_MAP["eDP-1:1"],
            "f",
        )
        self.assertEqual(
            actions,
            [
                "workspace-switch:3/eDP-1",
                "spawn:kitty",
                "workspace-switch:1/eDP-1",
                "window-focus:f",
            ],
        )


class TestSendMany(unittest.TestCase):
    def test_sends_all_actions_before_reading_replies(self):
        received: list[str] = []
        subscriptions: list[dict] = []

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "umbriel-test.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)

            def serve():
                conn, _addr = server.accept()
                stream = conn.makefile("rb")
                subscriptions.append(json.loads(stream.readline()))
                conn.sendall(b'{"data":null,"event":"submap"}\n')
                for _ in range(2):
                    request = json.loads(stream.readline())
                    received.append(request["arg"])
                    conn.sendall(b'{"ok":null}\n')
                stream.close()
                conn.close()
                server.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            with mock.patch.dict(os.environ, {"UMBRIEL_SOCKET": path}):
                ok = _send_many(["window-focus:a", "workspace-switch:2/eDP-1"])
            thread.join(timeout=2)

        self.assertTrue(ok)
        self.assertEqual(subscriptions[0]["cmd"], "subscribe")
        self.assertEqual(
            received, ["window-focus:a", "workspace-switch:2/eDP-1"]
        )


class _FakeSubscribe:
    def __init__(self, events):
        read_fd, write_fd = os.pipe()
        for event in events:
            os.write(write_fd, (json.dumps(event) + "\n").encode())
        os.close(write_fd)
        self.stdout = os.fdopen(read_fd, "r")

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


class TestRestoreFocusSequence(unittest.TestCase):
    def test_spawns_on_other_workspaces_and_tracks_the_original_focus(self):
        entries = [
            _entry("appone", "App One", index=1, focus_order=0),
            _entry("zen", "App Two", index=2, focus_order=1),
        ]
        for entry in entries:
            entry.cmd = "sh"
        session = Session(
            version=1,
            saved_at=0,
            compositor="umbriel",
            workspaces=[],
            windows=entries,
        )
        focus = _live("f", "kitty", "Terminal", "eDP-1:1")
        focus_active = {**focus, "active": True}
        new_temp = _live("w1", "appone", "App One", "eDP-1:3", active=True)
        new_other = _live("w2", "zen", "App Two", "eDP-1:2")
        event = {"event": "windows", "data": [focus, new_temp, new_other]}

        snapshots = [
            {"f": focus_active},
            {
                "f": focus_active,
                "w1": _live("w1", "appone", "App One", "eDP-1:1"),
                "w2": new_other,
            },
        ]

        def windows_snapshot():
            return snapshots.pop(0) if snapshots else {
                "f": focus_active,
                "w1": _live("w1", "appone", "App One", "eDP-1:1"),
                "w2": new_other,
            }

        batches = []

        def send_many(actions):
            batches.append(actions)
            return True

        with (
            mock.patch(
                "umbriel_session.restore.load_session", return_value=session
            ),
            mock.patch(
                "umbriel_session.restore.snapshot_windows",
                side_effect=windows_snapshot,
            ),
            mock.patch(
                "umbriel_session.restore.snapshot_workspaces",
                return_value=dict(WS_MAP),
            ),
            mock.patch(
                "umbriel_session.restore._send_many", side_effect=send_many
            ),
            mock.patch(
                "umbriel_session.restore.subprocess.Popen",
                return_value=_FakeSubscribe([event]),
            ),
            mock.patch("umbriel_session.restore.time.sleep"),
        ):
            report = restore_session(cfg=Config())

        self.assertEqual(
            batches[0],
            [
                "workspace-switch:3/eDP-1",
                "spawn:sh",
                "workspace-switch:1/eDP-1",
                "window-focus:f",
            ],
        )
        self.assertEqual(
            batches[1],
            [
                "workspace-switch:2/eDP-1",
                "spawn:sh",
                "workspace-switch:1/eDP-1",
                "window-focus:f",
            ],
        )
        self.assertEqual(
            batches[2],
            [
                "window-focus:w1",
                "window-move-to-workspace-silent:1/eDP-1",
                "window-focus:f",
            ],
        )
        self.assertEqual(batches[3], ["window-focus:f"])
        statuses = {entry.title: status for entry, status in report}
        self.assertEqual(statuses["App One"], "RESTORED")
        self.assertEqual(statuses["App Two"], "RESTORED")


if __name__ == "__main__":
    unittest.main()
