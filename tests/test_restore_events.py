"""Fragmented subscription frames cannot block matching deadlines."""

import json
import os
from select import select as real_select

import pytest

from umbriel_session import restore
from umbriel_session.restore import _read_window_snapshot as read_snapshot

from .test_restore_browser import _Clock, desktop, window


@pytest.fixture
def pipe():
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0) as reader, os.fdopen(write_fd, "wb", buffering=0) as writer:
        yield reader, writer


def frame(title="Document"):
    return (json.dumps({"event": "windows", "data": [{"id": "a", "title": title}]}, ensure_ascii=False) + "\n").encode()


def test_open_pipe_without_data_does_not_block(pipe):
    reader, writer = pipe
    assert read_snapshot(reader, bytearray()) == (None, False)
    writer.close()
    assert read_snapshot(reader, bytearray()) == (None, True)


def test_partial_json_frame_waits_for_remaining_bytes_without_blocking(pipe):
    reader, writer = pipe
    buffer = bytearray()
    event = frame()
    writer.write(event[:15])
    assert read_snapshot(reader, buffer) == (None, False)
    assert buffer == event[:15]
    writer.write(event[15:])
    assert read_snapshot(reader, buffer) == ({"a": {"id": "a", "title": "Document"}}, False)
    assert buffer == b""


def test_split_utf8_character_is_decoded_only_after_complete_line(pipe):
    reader, writer = pipe
    event = frame("🌙 Electron")
    split = event.index("🌙".encode()) + 1
    buffer = bytearray()
    writer.write(event[:split])
    assert read_snapshot(reader, buffer) == (None, False)
    writer.write(event[split:])
    snapshot, eof = read_snapshot(reader, buffer)
    assert not eof
    assert snapshot["a"]["title"] == "🌙 Electron"


def test_batched_frames_use_latest_complete_snapshot_and_keep_partial_tail(pipe):
    reader, writer = pipe
    buffer = bytearray()
    next_frame = frame("Third")
    writer.write(frame("First") + frame("Second") + next_frame[:10])
    snapshot, eof = read_snapshot(reader, buffer)
    assert not eof
    assert snapshot["a"]["title"] == "Second"
    assert buffer == next_frame[:10]
    writer.write(next_frame[10:])
    snapshot, _ = read_snapshot(reader, buffer)
    assert snapshot["a"]["title"] == "Third"


def test_invalid_and_unrelated_events_do_not_hide_valid_snapshot(pipe):
    reader, writer = pipe
    writer.write(b'bad json\n\xff\n[]\n{"event":"workspaces","data":[]}\n' + frame())
    snapshot, eof = read_snapshot(reader, bytearray())
    assert not eof
    assert snapshot["a"]["title"] == "Document"


def test_fragmented_electron_event_does_not_bypass_idle_timeout(desktop, monkeypatch, pipe):
    reader, writer = pipe
    for entry in desktop["entries"]:
        entry.app_id = "slack"
        entry.cmd = "slack"
    desktop["windows"].update({"a": window("a", "Slack", 1, app="slack"),
                                "b": window("b", "Slack", 2, app="slack")})
    desktop["cfg"].load_time = 60
    desktop["cfg"].window_wait_timeout = 2
    desktop["proc"].stdout = reader
    writer.write(b'{"event":"windows","data":[')
    clock = _Clock()

    def select_partial(readers, writers, errors, timeout):
        clock.now += min(timeout, 0.25)
        return real_select(readers, writers, errors, 0)

    monkeypatch.setattr(restore, "_read_window_snapshot", read_snapshot)
    monkeypatch.setattr(restore.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(restore.select, "select", select_partial)
    report = restore.restore_session(cfg=desktop["cfg"])
    assert clock.now == pytest.approx(2)
    assert sum(status == "AMBIGUOUS" for _, status in report) == 2
    assert not writer.closed  # No EOF was needed to escape the partial frame.
    desktop["proc"].terminate.assert_called_once()
