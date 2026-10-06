"""Integer-pixel translation of one island's texels.

The source is read from a frozen copy of the island's own region, taken before any write, so a
destination that overlaps the source still reads original texels. Texels are written only inside
the write mask: the island shifted by the translation, plus an optional margin generated from
the island's own texels. Whatever the write mask covers is overwritten, including texels used by
other UVs. Nothing wraps around the image tile, and no full-image copy is required.
"""

from dataclasses import dataclass

import numpy as np

from .raster import COVERAGE_RULE, PixelRegion, bands, cover, degenerate_triangles, dilate, grow, is_integer, tile

PIXEL_TOL = 1e-4            # distance from a whole texel still accepted as an integer translation
TRANSLATION_TOL = 1e-6      # UV units; largest per-corner deviation from a uniform translation


class TransferError(ValueError):
    """Raised before any write; `category` names the kind of error."""

    def __init__(self, category, message, **detail):
        super().__init__(message)
        self.category = category
        self.detail = detail


@dataclass(frozen=True, eq=False)
class TranslationPlan:
    width: int
    height: int
    dx: int
    dy: int
    margin: int
    source: PixelRegion        # bounding box of the island's covered texels at the origin
    island: np.ndarray         # bool mask over `source`
    write: PixelRegion         # destination region (island + margin), clipped to the tile
    write_mask: np.ndarray     # bool mask over `write`
    margin_mask: np.ndarray    # bool mask over `write`, the margin part of write_mask
    overlap_texels: int        # destination island texels that are also source island texels
    clipped_margin_texels: int
    coverage_rule: str = COVERAGE_RULE

    @property
    def interior_texels(self):
        return int(self.island.sum())

    @property
    def written_texels(self):
        return int(self.write_mask.sum())

    def frozen_bytes(self, channels, itemsize=4):
        return self.source.area * channels * itemsize

    def write_bytes(self, channels, itemsize=4):
        return self.write.area * channels * itemsize


def measure_translation(origin_uv, final_uv):
    """(du, dv, largest deviation from a uniform translation) between two corner UV arrays."""
    d = np.asarray(final_uv, np.float64) - np.asarray(origin_uv, np.float64)
    mean = d.mean(axis=0)
    return float(mean[0]), float(mean[1]), float(np.abs(d - mean).max()) if len(d) else 0.0


def to_whole_texels(du, dv, width, height):
    """Nearest whole-texel translation: (dx, dy, du', dv', already_whole)."""
    fx, fy = du * width, dv * height
    dx, dy = int(round(fx)), int(round(fy))
    whole = is_integer(fx, PIXEL_TOL) and is_integer(fy, PIXEL_TOL)
    return dx, dy, dx / width, dy / height, whole


def plan_translation(tri_uv, width, height, dx, dy, margin=0):
    """Plan the transfer of the island given by its origin UV triangles (T, 3, 2). No writes."""
    if not (isinstance(dx, (int, np.integer)) and isinstance(dy, (int, np.integer))):
        raise TransferError("InvalidTransform", "only whole-texel translations are copied", dx=dx, dy=dy)
    if margin < 0:
        raise TransferError("InvalidTransform", "margin must be zero or positive", margin=margin)
    bad = degenerate_triangles(tri_uv, width, height)
    if len(bad):
        raise TransferError("SelectionError", "island has degenerate UV triangles", triangles=bad.tolist())
    source, island = cover(tri_uv, width, height)
    if source.is_empty:
        raise TransferError("SelectionError", f"island covers no texel center at {width}x{height}")
    full = tile(width, height)
    dest = source.translate(int(dx), int(dy))
    if not full.contains(dest):
        raise TransferError("InvalidTransform", "island would leave the image tile", destination=dest.__dict__)
    canvas = source.expand(margin)
    grown = dilate(island, margin)                       # island + margin, over `canvas`
    ring = grown.copy()
    ring[margin:margin + island.shape[0], margin:margin + island.shape[1]] &= ~island
    placed = canvas.translate(int(dx), int(dy))
    write = placed.intersect(full)
    window = write.slices(placed)
    write_mask, margin_mask = grown[window].copy(), ring[window].copy()
    both = PixelRegion(min(source.x0, dest.x0), min(source.y0, dest.y0),
                       max(source.x1, dest.x1), max(source.y1, dest.y1))
    a = np.zeros((both.height, both.width), bool)
    b = np.zeros_like(a)
    a[source.slices(both)] = island
    b[dest.slices(both)] = island
    return TranslationPlan(width, height, int(dx), int(dy), margin, source, island, write, write_mask,
                           margin_mask, int((a & b).sum()), int(ring.sum() - margin_mask.sum()))


def apply_translation(plan, frozen_source, destination):
    """Patch for `plan.write`: the island copied from `frozen_source` (over `plan.source`), the
    margin extended from the island's own texels, and `destination` left as is elsewhere."""
    frozen_source = np.asarray(frozen_source)
    if frozen_source.shape[:2] != (plan.source.height, plan.source.width):
        raise ValueError("frozen_source does not cover plan.source")
    if np.asarray(destination).shape[:2] != (plan.write.height, plan.write.width):
        raise ValueError("destination does not cover plan.write")
    patch = np.array(destination, copy=True)
    dest = plan.source.translate(plan.dx, plan.dy)
    patch[dest.slices(plan.write)][plan.island] = frozen_source[plan.island]
    if plan.margin and plan.margin_mask.any():
        m = plan.margin
        placed = plan.source.expand(m).translate(plan.dx, plan.dy)     # the island grown by m rings, moved
        oy, ox = placed.y0 - plan.write.y0, plan.write.x0 - placed.x0
        h, w = plan.island.shape
        for r0, r1, lo, hi in bands(h + 2 * m, w + 2 * m, m):          # rows of the grown island
            values, have = _padded_rows(frozen_source, plan.island, m, lo, hi)
            grown, _ = grow(values, have, m)
            w0, w1 = max(r0 + oy, 0), min(r1 + oy, plan.write.height)      # the same rows in plan.write
            if w0 < w1:
                ring = plan.margin_mask[w0:w1]
                rows = grown[w0 - oy - lo:w1 - oy - lo, ox:ox + plan.write.width]
                patch[w0:w1][ring] = rows[ring].astype(patch.dtype)
    return patch


def _padded_rows(values, known, steps, lo, hi):
    """Rows lo..hi of `values` and `known` padded with `steps` empty texels on each side, as float64
    values and the known mask: what raster.grow extends `steps` rings from the island texels."""
    h, w = known.shape
    band = np.zeros((hi - lo, w + 2 * steps) + values.shape[2:], np.float64)
    have = np.zeros((hi - lo, w + 2 * steps), bool)
    s0, s1 = max(lo - steps, 0), min(hi - steps, h)
    if s0 < s1:
        k = known[s0:s1]
        band[s0 + steps - lo:s1 + steps - lo, steps:steps + w][k] = values[s0:s1][k]
        have[s0 + steps - lo:s1 + steps - lo, steps:steps + w] = k
    return band, have
