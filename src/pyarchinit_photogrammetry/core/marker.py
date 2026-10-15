"""Markers (control/check points, coded targets) and scale bars."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .camera import Reference


@dataclass
class Projection:
    """Observation of a marker in a photo, in pixels."""

    x: float
    y: float
    pinned: bool = False  # True when placed/confirmed manually

    def to_list(self) -> list:
        return [self.x, self.y, self.pinned]


@dataclass
class Marker:
    """A point identified in several photos.

    ``position`` is the triangulated position in chunk coordinates.
    ``reference`` holds the surveyed coordinates (GCP) in the chunk CRS.
    ``code`` is the decoded target ID for coded targets (``None`` for manual markers).
    """

    key: int
    label: str
    code: int | None = None
    target_type: str | None = None
    projections: dict[int, Projection] = field(default_factory=dict)  # camera key -> projection
    position: np.ndarray | None = None
    reference: Reference = field(default_factory=Reference)
    checkpoint: bool = False  # used only to assess accuracy, not for georeferencing

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "code": self.code, "target_type": self.target_type,
            "projections": {str(k): p.to_list() for k, p in self.projections.items()},
            "position": None if self.position is None else np.asarray(self.position).tolist(),
            "reference": self.reference.to_dict(), "checkpoint": self.checkpoint,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Marker":
        pos = d.get("position")
        return cls(
            key=d["key"], label=d["label"], code=d.get("code"), target_type=d.get("target_type"),
            projections={int(k): Projection(*v) for k, v in d.get("projections", {}).items()},
            position=None if pos is None else np.asarray(pos, float),
            reference=Reference.from_dict(d.get("reference")), checkpoint=d.get("checkpoint", False),
        )


@dataclass
class ScaleBar:
    """Known distance between two markers (metres)."""

    key: int
    label: str
    marker_a: int
    marker_b: int
    distance: float
    accuracy: float = 0.001
    enabled: bool = True

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "ScaleBar":
        return cls(**d)
