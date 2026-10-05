"""Inverse-mapped transfer of an island's texels under an affine map: rotation and scale.

Every destination texel covered by the island's final triangles (coverage rule center-v1) is mapped
back to the origin and sampled bilinearly from a frozen copy of the source taken before any write.
Taps read only the island's own texels: the source is first grown by EXTEND rings from the island
texels, so a tap just outside the island reads a value made of island texels, never an unrelated
atlas texel (source-safe filtering). A sample whose taps all miss the grown source takes the
nearest island texel. The margin is grown from the written island texels. Whatever the write mask
covers is overwritten, including texels used by other UVs (D1). Nothing wraps around the tile.
"""

from dataclasses import dataclass

import numpy as np

from .affine import REFUSALS
from .raster import COVERAGE_RULE, PixelRegion, bands, cover, degenerate_triangles, dilate, grow, tile
from .translation import TransferError

EXTEND = 2          # texel rings grown around the source island for bilinear taps
SNAP = 1e-6         # a sample this close to a texel center reads that texel alone


@dataclass(frozen=True, eq=False)
class AffinePlan:
    width: int
    height: int
    matrix: np.ndarray          # destination texel -> source texel, (2, 2)
    offset: np.ndarray          # (2,)
    margin: int
    source: PixelRegion         # frozen region: the island's texels at the origin plus EXTEND rings, in the tile
    island: np.ndarray          # bool over `source`
    dest: PixelRegion           # bounding box of the island's texels at the final position
    dest_mask: np.ndarray       # bool over `dest`
    write: PixelRegion          # dest + margin, clipped to the tile
    write_mask: np.ndarray      # bool over `write`
    margin_mask: np.ndarray     # bool over `write`, the margin part of write_mask
    overlap_texels: int         # destination island texels that are also source island texels
    coverage_rule: str = COVERAGE_RULE

    @property
    def interior_texels(self):
        return int(self.dest_mask.sum())

    def frozen_bytes(self, channels, itemsize=4):
        return self.source.area * channels * itemsize

    def write_bytes(self, channels, itemsize=4):
        return self.write.area * channels * itemsize


def plan_affine(origin_tri_uv, final_tri_uv, fit, width, height, margin=0):
    """Plan the resampled transfer of an island from its origin triangles to its final ones, (T, 3, 2)
    each, under `fit` (affine.fit_affine). No writes."""
    if not fit.ok:
        raise TransferError("InvalidTransform", REFUSALS[fit.verdict], verdict=fit.verdict)
    if margin < 0:
        raise TransferError("InvalidTransform", "margin must be zero or positive", margin=margin)
    for tri, where in ((origin_tri_uv, "origin"), (final_tri_uv, "final position")):
        bad = degenerate_triangles(tri, width, height)
        if len(bad):
            raise TransferError("SelectionError", f"island has degenerate UV triangles at its {where}",
                                triangles=bad.tolist())
        uv = np.asarray(tri, np.float64)
        if uv.size and (uv.min() < 0.0 or uv.max() > 1.0):
            raise TransferError("InvalidTransform", f"island would leave the image tile at its {where}",
                                uv_min=float(uv.min()), uv_max=float(uv.max()))
    src_region, island = cover(origin_tri_uv, width, height)
    if src_region.is_empty:
        raise TransferError("SelectionError", f"island covers no texel center at {width}x{height}")
    dest, dest_mask = cover(final_tri_uv, width, height)
    if dest.is_empty:
        raise TransferError("InvalidTransform", "island covers no texel center at its final position")
    full = tile(width, height)
    source = src_region.expand(EXTEND).intersect(full)
    island_in_source = np.zeros((source.height, source.width), bool)
    island_in_source[src_region.slices(source)] = island
    canvas = dest.expand(margin)
    grown = dilate(dest_mask, margin)
    ring = grown.copy()
    ring[margin:margin + dest_mask.shape[0], margin:margin + dest_mask.shape[1]] &= ~dest_mask
    write = canvas.intersect(full)
    window = write.slices(canvas)
    both = PixelRegion(min(src_region.x0, dest.x0), min(src_region.y0, dest.y0),
                       max(src_region.x1, dest.x1), max(src_region.y1, dest.y1))
    a = np.zeros((both.height, both.width), bool)
    b = np.zeros_like(a)
    a[src_region.slices(both)] = island
    b[dest.slices(both)] = dest_mask
    m, t = fit.pixel_map(width, height)
    inverse = np.linalg.inv(m)
    return AffinePlan(width, height, inverse, -inverse @ t, margin, source, island_in_source, dest, dest_mask,
                      write, grown[window].copy(), ring[window].copy(), int((a & b).sum()))


def sample_positions(plan):
    """Source texel coordinates, relative to plan.source, of every destination island texel in
    np.nonzero(plan.dest_mask) order, as (rows, cols, positions (N, 2) as x, y)."""
    ys, xs = np.nonzero(plan.dest_mask)
    q = np.stack([xs + plan.dest.x0, ys + plan.dest.y0], axis=1).astype(np.float64)
    s = q @ plan.matrix.T + plan.offset - np.array([plan.source.x0, plan.source.y0], np.float64)
    near = np.round(s)
    close = np.abs(s - near) <= SNAP
    s[close] = near[close]
    return ys, xs, s


def apply_affine(plan, frozen_source, destination, island_values=None):
    """Patch for `plan.write`: the island resampled from `frozen_source` (over `plan.source`), the margin
    grown from the written island texels, and `destination` left as is elsewhere. Returns (patch, in the
    destination's dtype, number of samples that fell back to the nearest island texel).

    The samples and the margin are computed in row bands of plan.write (raster.bands), each with `margin`
    rows of context, so the memory they take does not grow with the island. `island_values(values, rows,
    cols)`, when given, replaces the island texels' resampled values, as the destination stores them, at
    (rows, cols) of plan.write, before the margin grows from them."""
    frozen = np.asarray(frozen_source)
    destination = np.asarray(destination)
    if frozen.shape[:2] != (plan.source.height, plan.source.width):
        raise ValueError("frozen_source does not cover plan.source")
    if destination.shape[:2] != (plan.write.height, plan.write.width):
        raise ValueError("destination does not cover plan.write")
    values, have = grow(frozen, plan.island, EXTEND)
    ys, xs, s = sample_positions(plan)
    rows, cols = ys + plan.dest.y0 - plan.write.y0, xs + plan.dest.x0 - plan.write.x0
    keep = (rows >= 0) & (rows < plan.write.height) & (cols >= 0) & (cols < plan.write.width)
    rows, cols, s = rows[keep], cols[keep], s[keep]            # row by row, as np.nonzero gives them
    margin = plan.margin if plan.margin and plan.margin_mask.any() else 0
    patch = destination.copy()
    fallback = 0
    for r0, r1, lo, hi in bands(plan.write.height, plan.write.width, margin):
        i0, i1 = np.searchsorted(rows, [lo, hi])
        r, c = rows[i0:i1], cols[i0:i1]
        sampled, missing = _bilinear(s[i0:i1], values, have, frozen, plan.island)
        if island_values is not None:
            sampled = island_values(sampled.astype(destination.dtype).astype(np.float64), r, c)
        own = (r >= r0) & (r < r1)
        fallback += int(missing[own].sum())
        if margin:
            band = destination[lo:hi].astype(np.float64)
            band[r - lo, c] = sampled
            known = np.zeros((hi - lo, plan.write.width), bool)
            known[r - lo, c] = True
            grown, _ = grow(band, known, margin)
            ring = plan.margin_mask[r0:r1]
            patch[r0:r1][ring] = grown[r0 - lo:r1 - lo][ring]
        patch[r[own], c[own]] = sampled[own]
    return patch, fallback


def _bilinear(s, values, have, frozen, island):
    """Samples at source positions `s` ((N, 2) as x, y) from the grown source (`values`, `have`), and which
    of them fell back to the nearest island texel of `frozen`, as (sampled (N, C) float64, missing (N,))."""
    x0 = np.floor(s[:, 0]).astype(np.int64)
    y0 = np.floor(s[:, 1]).astype(np.int64)
    fx, fy = s[:, 0] - x0, s[:, 1] - y0
    H, W = have.shape
    acc = np.zeros((len(s),) + values.shape[2:], np.float64)
    total = np.zeros(len(s), np.float64)
    for dy, dx, w in ((0, 0, (1 - fx) * (1 - fy)), (0, 1, fx * (1 - fy)), (1, 0, (1 - fx) * fy), (1, 1, fx * fy)):
        xi, yi = x0 + dx, y0 + dy
        ok = (w > 0) & (xi >= 0) & (xi < W) & (yi >= 0) & (yi < H)
        ok[ok] = have[yi[ok], xi[ok]]
        acc[ok] += (w[ok] * values[yi[ok], xi[ok]].T).T
        total[ok] += w[ok]
    missing = total <= 0
    sampled = np.zeros_like(acc)
    sampled[~missing] = (acc[~missing].T / total[~missing]).T
    if missing.any():
        iy, ix = np.nonzero(island)
        for k in np.flatnonzero(missing):
            n = int(np.argmin((ix - s[k, 0]) ** 2 + (iy - s[k, 1]) ** 2))
            sampled[k] = frozen[iy[n], ix[n]]
    return sampled, missing
