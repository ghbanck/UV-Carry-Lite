"""In-memory transaction of a carry: the image patches and the island's UVs are published together, or
whatever was published is taken back.

Every patch is computed before the first write; publishing only copies them in, in plan order, then
writes the UVs. Before each image write, `revalidate` checks that the context and the images not yet
written still match the session fingerprints. When any step fails, the writes already made are
rolled back from their snapshots, newest first, and the result says whether that restoration is
complete. A rollback that fails is the most severe outcome and is never reported as success.

Faults is the failure-injection hook of the tests: an armed point
raises InjectedFault when the transaction reaches it. Nothing is armed in normal use.
"""

from dataclasses import dataclass, field

import numpy as np


class InjectedFault(RuntimeError):
    """Raised at an armed failure point."""


class ContextChanged(RuntimeError):
    def __init__(self, changed):
        super().__init__("context changed: " + ", ".join(changed))
        self.changed = list(changed)


class Faults:
    """Named failure points, e.g. "after_image:0" (right after the first image write), "before_uvs",
    "rollback_image:0". An armed point fires once."""

    def __init__(self):
        self.armed = set()
        self.hits = []

    def arm(self, *points):
        self.armed.update(points)

    def clear(self):
        self.armed.clear()
        self.hits.clear()

    def check(self, point):
        if point in self.armed:
            self.armed.discard(point)
            self.hits.append(point)
            raise InjectedFault(f"injected failure at {point}")


@dataclass(frozen=True, eq=False)
class ImagePatch:
    target: str                 # image identity
    region: object              # PixelRegion written
    before: np.ndarray          # texels of `region` before the write: the rollback snapshot
    after: np.ndarray           # texels to publish

    @property
    def nbytes(self):
        return self.before.nbytes + self.after.nbytes


@dataclass
class OperationResult:
    state: str                  # "COMMITTED" or "FAILED"
    published: list             # targets written and kept, in order; empty after a failure
    category: str = None        # ContextChanged, CommitFailure or RollbackFailure
    stage: str = None           # where it failed: "revalidate:<i>", "image:<i>" or "uvs"
    error: str = None
    rollback: str = None        # None (nothing was written), "complete" or "failed: <what>"
    changed: list = field(default_factory=list)     # fingerprint keys, for ContextChanged

    @property
    def ok(self):
        return self.state == "COMMITTED"


def publish(patches, write_image, write_uvs=None, restore_uvs=None, revalidate=None, faults=None):
    """Publish `patches` (ImagePatch, in order) through write_image(target, region, texels), then the
    UVs through write_uvs(). revalidate(i) returns the fingerprint keys that changed before patch i is
    written (empty when the context still matches). restore_uvs() puts the UVs back as they were
    before write_uvs. Returns an OperationResult."""
    if write_uvs is not None and restore_uvs is None:
        raise ValueError("write_uvs needs restore_uvs")
    faults = faults if faults is not None else Faults()
    done, uvs_touched, stage = [], False, None
    try:
        for i, patch in enumerate(patches):
            stage = f"revalidate:{i}"
            changed = revalidate(i) if revalidate is not None else []
            if changed:
                raise ContextChanged(changed)
            stage = f"image:{i}"
            faults.check(f"before_image:{i}")
            done.append(patch)              # before the write: one that fails half way is rolled back too
            write_image(patch.target, patch.region, patch.after)
            faults.check(f"after_image:{i}")
        if write_uvs is not None:
            stage = "uvs"
            faults.check("before_uvs")
            uvs_touched = True          # a UV write that fails half way may have changed some UVs
            write_uvs()
            faults.check("after_uvs")
    except Exception as exc:            # every failure is rolled back and reported, never hidden
        failed = []
        if uvs_touched:
            try:
                faults.check("rollback_uvs")
                restore_uvs()
            except Exception as err:
                failed.append(f"UVs ({err})")
        for k in range(len(done) - 1, -1, -1):
            patch = done[k]
            try:
                faults.check(f"rollback_image:{k}")
                write_image(patch.target, patch.region, patch.before)
            except Exception as err:
                failed.append(f"{patch.target} ({err})")
        if failed:
            category, rollback = "RollbackFailure", "failed: " + "; ".join(failed)
        else:
            category = "ContextChanged" if isinstance(exc, ContextChanged) else "CommitFailure"
            rollback = "complete" if (done or uvs_touched) else None
        return OperationResult("FAILED", [], category, stage, f"{type(exc).__name__}: {exc}", rollback,
                               getattr(exc, "changed", []))
    return OperationResult("COMMITTED", [p.target for p in patches])
