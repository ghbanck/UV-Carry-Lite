"""Padding of islands that stay where they are.

Ctrl+Enter with complete islands selected and no move fills the texels around them that no UV uses, so that
filtering and mip levels read the islands' own colours past their edges. In each image, the texels within
`margin` 8-neighbour steps of the islands, stepping only through texels that no UV triangle of the mesh covers
(coverage rule center-v1), are filled from the islands' own texels ring by ring, each the mean of its filled
neighbours (raster.grow): between two islands a texel takes the nearer one, or both where they are as near.
A texel that a UV uses is never written, the islands' own included, and the padding never spreads through one.
Nothing wraps around the image tile.
"""

from dataclasses import dataclass

import numpy as np

from .raster import COVERAGE_RULE, PixelRegion, bands, cover, degenerate_triangles, grow, rasterize, reach, tile
from .translation import TransferError


@dataclass(frozen=True, eq=False)
class PaddingPlan:
    width: int
    height: int
    margin: int
    write: PixelRegion          # the islands' texels grown by `margin`, inside the tile
    island: np.ndarray          # bool over `write`: texels the islands cover, the padding's source
    write_mask: np.ndarray      # bool over `write`: the texels to fill
    coverage_rule: str = COVERAGE_RULE

    @property
    def source(self):
        return self.write

    @property
    def margin_mask(self):
        """Every texel a padding writes is grown, as a carry's margin is."""
        return self.write_mask

    @property
    def interior_texels(self):
        return 0

    @property
    def overlap_texels(self):
        return 0

    @property
    def padded_texels(self):
        return int(self.write_mask.sum())

    def frozen_bytes(self, channels, itemsize=4):
        return 0

    def write_bytes(self, channels, itemsize=4):
        return self.write.area * channels * itemsize


def plan_padding(island_tri_uv, other_tri_uv, width, height, margin):
    """Plan the padding of islands given by their UV triangles, (T, 3, 2), where they are, at width x height.
    `other_tri_uv` holds every other UV triangle of the mesh, (T, 3, 2): the texels they cover are never
    written. No writes."""
    if margin < 0:
        raise TransferError("InvalidTransform", "margin must be zero or positive", margin=margin)
    bad = degenerate_triangles(island_tri_uv, width, height)
    if len(bad):
        raise TransferError("SelectionError", "island has degenerate UV triangles", triangles=bad.tolist())
    source, island = cover(island_tri_uv, width, height)
    if source.is_empty:
        raise TransferError("SelectionError", f"islands cover no texel center at {width}x{height}")
    write = source.expand(margin).intersect(tile(width, height))
    mine = np.zeros((write.height, write.width), bool)
    mine[source.slices(write)] = island
    used = mine.copy()
    if len(other_tri_uv):
        region, mask = rasterize(other_tri_uv, width, height, clip=write)
        if not region.is_empty:
            used[region.slices(write)] |= mask
    return PaddingPlan(width, height, margin, write, mine, reach(mine, ~used, margin))


def apply_padding(plan, destination):
    """Patch for plan.write: the texels of plan.write_mask filled from the islands' texels in `destination`,
    every other texel as it is. The rings grow in row bands of plan.write (raster.bands), each with `margin`
    rows of context, which gives the rows of a band as over the whole region."""
    destination = np.asarray(destination)
    if destination.shape[:2] != (plan.write.height, plan.write.width):
        raise ValueError("destination does not cover plan.write")
    patch = destination.copy()
    m = plan.margin
    if not m or not plan.write_mask.any():
        return patch
    for r0, r1, lo, hi in bands(plan.write.height, plan.write.width, m):
        ring = plan.write_mask[r0:r1]
        if not ring.any():
            continue
        grown, _ = grow(destination[lo:hi], plan.island[lo:hi], m, fill=plan.write_mask[lo:hi])
        patch[r0:r1][ring] = grown[r0 - lo:r1 - lo][ring].astype(patch.dtype)
    return patch
