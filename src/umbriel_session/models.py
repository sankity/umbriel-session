"""Data structures for the session file."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class WorkspaceRef:
    output: str
    index: int
    named: bool
    name: str


@dataclass
class WindowEntry:
    app_id: str
    title: str
    cmd: str
    cwd: str | None
    pid: int
    workspace: WorkspaceRef
    output: str
    floating: bool
    x: int
    y: int
    w: int
    h: int
    fullscreen: bool
    maximized: bool
    maximized_to_edges: bool
    pinned: bool
    focused: bool
    focus_order: int
    content_type: str
    xdg_tag: str
    xwayland: bool
    tabbed: bool
    scratchpad: str

    @classmethod
    def from_compositor(
        cls, w: dict, focus_order: int, workspace: WorkspaceRef
    ) -> WindowEntry:
        return cls(
            app_id=w.get("app_id", ""),
            title=w.get("title", ""),
            cmd="",
            cwd=None,
            pid=w.get("pid", -1),
            workspace=workspace,
            output=w.get("output", ""),
            floating=w.get("floating", False),
            x=w.get("x", 0),
            y=w.get("y", 0),
            w=w.get("w", 0),
            h=w.get("h", 0),
            fullscreen=w.get("fullscreen", False),
            maximized=w.get("maximized", False),
            maximized_to_edges=w.get("maximized_to_edges", False),
            pinned=w.get("pinned", False),
            focused=w.get("focused", False),
            focus_order=focus_order,
            content_type=w.get("content_type", "none"),
            xdg_tag=w.get("xdg_tag", ""),
            xwayland=w.get("xwayland", False),
            tabbed=w.get("tabbed", False),
            scratchpad=w.get("scratchpad", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WindowEntry:
        d = dict(d)
        d["workspace"] = WorkspaceRef(**d["workspace"])
        return cls(**d)


@dataclass
class WorkspaceEntry:
    id: str
    name: str
    named: bool
    index: int
    output: str
    layout: str
    layout_override: bool

    @classmethod
    def from_compositor(cls, w: dict) -> WorkspaceEntry:
        return cls(
            id=w["id"],
            name=w["name"],
            named=w["named"],
            index=w["index"],
            output=w["output"],
            layout=w["layout"],
            layout_override=w["layout_override"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorkspaceEntry:
        return cls(**d)


@dataclass
class Session:
    version: int
    saved_at: float
    compositor: str
    workspaces: list[WorkspaceEntry]
    windows: list[WindowEntry]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "saved_at": self.saved_at,
            "compositor": self.compositor,
            "workspaces": [w.to_dict() for w in self.workspaces],
            "windows": [w.to_dict() for w in self.windows],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Session:
        return cls(
            version=d["version"],
            saved_at=d["saved_at"],
            compositor=d["compositor"],
            workspaces=[WorkspaceEntry.from_dict(w) for w in d["workspaces"]],
            windows=[WindowEntry.from_dict(w) for w in d["windows"]],
        )


def workspace_ref_from_id(ws_id: str) -> WorkspaceRef | None:
    """Parse an Umbriel workspace id like ``HDMI-A-1:3`` into a ref.

    Returns None when the id has an unexpected shape.
    """
    if not ws_id:
        return None
    output, _, serial = ws_id.rpartition(":")
    if not output or not serial.isdigit():
        return None
    return WorkspaceRef(output=output, index=int(serial), named=False, name=str(serial))
