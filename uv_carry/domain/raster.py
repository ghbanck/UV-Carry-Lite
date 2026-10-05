"""Deterministic rasterization of UV triangles into texel masks.

Coverage rule "center-v1": texel (i, j) of a W x H image has its center at
((i + 0.5) / W, (j + 0.5) / H) in UV space, with row 0 at v = 0 (Blender's image origin).
A texel is covered by a triangle when its center lies inside the triangle or on one of its
edges, tested with barycentric coordinates >= -COVERAGE_EPS computed in pixel units. A mask is
the union of the covered texels of its triangles, so concavities and holes stay uncovered.
"""

import math
from dataclasses import dataclass

import numpy as np

COVERAGE_RULE = "center-v1"
COVERAGE_EPS = 1e-9
DEGENERATE_AREA = 1e-12   # twice the triangle area in pixel units below which a triangle is degenerate
CHUNK = 1 << 20           # texel centers tested at once
BAND = 1 << 18            # texels per row band of a computation done band by band, context rows aside


@dataclass(frozen=True)
class PixelRegion:
    """Half-open texel rectangle [x0, x1) x [y0, y1)."""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self):
        return max(self.x1 - self.x0, 0)

    @property
    def height(self):
        return max(self.y1 - self.y0, 0)

    @property
    def area(self):
        return self.width * self.height

    @property
    def is_empty(self):
        return self.area == 0

    def translate(self, dx, dy):
        return PixelRegion(self.x0 + dx, self.y0 + dy, self.x1 + dx, self.y1 + dy)

    def expand(self, n):
        return PixelRegion(self.x0 - n, self.y0 - n, self.x1 + n, self.y1 + n)

    def intersect(self, other):
        return PixelRegion(max(self.x0, other.x0), max(self.y0, other.y0),
                           min(self.x1, other.x1), min(self.y1, other.y1))

    def contains(self, other):
        return (self.x0 <= other.x0 and self.y0 <= other.y0
                and self.x1 >= other.x1 and self.y1 >= other.y1)

    def slices(self, within):
        """Index slices of this region inside an array that covers `within`."""
        return (slice(self.y0 - within.y0, self.y1 - within.y0), slice(self.x0 - within.x0, self.x1 - within.x0))


def tile(width, height):
    return PixelRegion(0, 0, width, height)


def to_pixels(tri_uv, width, height):
    """UV triangles (T, 3, 2) to pixel units, float64."""
    return np.asarray(tri_uv, np.float64) * np.array([width, height], np.float64)


def degenerate_triangles(tri_uv, width, height):
    """Indices of triangles whose UV area is (numerically) zero at this resolution."""
    p = to_pixels(tri_uv, width, height)
    e0, e1 = p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]
    twice_area = np.abs(e0[:, 0] * e1[:, 1] - e1[:, 0] * e0[:, 1])
    return np.flatnonzero(twice_area < DEGENERATE_AREA)


def covered(p, bounds):
    """Texel centers of `bounds` that each triangle of `p` ((T, 3, 2), pixel units) covers, as arrays
    (triangle, x, y, l0, l1, l2), in triangle order and row by row within each triangle's box. The
    centers of many triangles are tested at once, CHUNK at most; the arithmetic is the per-triangle one."""
    a = p[:, 0]
    e0, e1 = p[:, 1] - a, p[:, 2] - a
    den = e0[:, 0] * e1[:, 1] - e1[:, 0] * e0[:, 1]
    lo = np.floor(p.min(axis=1) - 0.5).astype(np.int64)
    hi = np.ceil(p.max(axis=1) - 0.5).astype(np.int64) + 1
    x0, y0 = np.maximum(lo[:, 0], bounds.x0), np.maximum(lo[:, 1], bounds.y0)
    w = np.maximum(np.minimum(hi[:, 0], bounds.x1) - x0, 0)
    h = np.maximum(np.minimum(hi[:, 1], bounds.y1) - y0, 0)
    tris = np.flatnonzero((np.abs(den) >= DEGENERATE_AREA) & (w > 0) & (h > 0))
    sizes = (w * h)[tris]
    ends = np.cumsum(sizes)
    start = 0
    while start < len(tris):
        stop = max(int(np.searchsorted(ends, (ends[start - 1] if start else 0) + CHUNK, side="right")), start + 1)
        chunk, n = tris[start:stop], sizes[start:stop]
        t = np.repeat(chunk, n)
        k = np.arange(len(t)) - np.repeat(np.cumsum(n) - n, n)
        xs, ys = x0[t] + k % w[t], y0[t] + k // w[t]
        px, py = (xs + 0.5) - a[t, 0], (ys + 0.5) - a[t, 1]
        l1 = (px * e1[t, 1] - e1[t, 0] * py) / den[t]
        l2 = (e0[t, 0] * py - px * e0[t, 1]) / den[t]
        l0 = 1.0 - l1 - l2
        inside = (l0 >= -COVERAGE_EPS) & (l1 >= -COVERAGE_EPS) & (l2 >= -COVERAGE_EPS)
        yield t[inside], xs[inside], ys[inside], l0[inside], l1[inside], l2[inside]
        start = stop


def rasterize(tri_uv, width, height, clip=None):
    """Covered texels of the union of triangles, as (region, bool mask over region).

    The region is the bounding box of the covered texels, intersected with `clip` (default: the
    image tile). Degenerate triangles cover nothing; callers decide whether that is an error.
    """
    clip = tile(width, height) if clip is None else clip.intersect(tile(width, height))
    p = to_pixels(tri_uv, width, height)
    if len(p) == 0 or clip.is_empty:
        return PixelRegion(0, 0, 0, 0), np.zeros((0, 0), bool)
    lo = np.floor(p.min(axis=1) - 0.5).astype(np.int64)
    hi = np.ceil(p.max(axis=1) - 0.5).astype(np.int64) + 1
    bx0, by0 = max(int(lo[:, 0].min()), clip.x0), max(int(lo[:, 1].min()), clip.y0)
    bx1, by1 = min(int(hi[:, 0].max()), clip.x1), min(int(hi[:, 1].max()), clip.y1)
    bounds = PixelRegion(bx0, by0, max(bx1, bx0), max(by1, by0))
    mask = np.zeros((bounds.height, bounds.width), bool)
    if bounds.is_empty:
        return PixelRegion(0, 0, 0, 0), np.zeros((0, 0), bool)
    for _, xs, ys, _, _, _ in covered(p, bounds):
        mask[ys - bounds.y0, xs - bounds.x0] = True
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return PixelRegion(0, 0, 0, 0), np.zeros((0, 0), bool)
    tight = PixelRegion(bounds.x0 + int(xs.min()), bounds.y0 + int(ys.min()),
                        bounds.x0 + int(xs.max()) + 1, bounds.y0 + int(ys.max()) + 1)
    return tight, mask[tight.slices(bounds)].copy()


def cover(tri_uv, width, height):
    """rasterize() over the tile, or, when the triangles cover no texel center (an island thinner than a texel
    at this size), the one texel under their centroid: what a renderer samples there."""
    region, mask = rasterize(tri_uv, width, height)
    tri = np.asarray(tri_uv, np.float64).reshape(-1, 2)
    if not region.is_empty or not len(tri):
        return region, mask
    c = tri.mean(axis=0)
    x = int(np.clip(np.floor(c[0] * width), 0, width - 1))
    y = int(np.clip(np.floor(c[1] * height), 0, height - 1))
    return PixelRegion(x, y, x + 1, y + 1), np.ones((1, 1), bool)


def rasterize_ids(tri_uv, width, height, clip=None):
    """rasterize(), and for every texel of the region: the first triangle whose coverage includes its
    center (-1 where none does), the barycentric coordinates of the center in that triangle, and how many
    triangles cover it. Returns (region, tri (H, W) int, bary (H, W, 3), count (H, W) int); the region and
    the mask (tri >= 0) equal those of rasterize()."""
    region, mask = rasterize(tri_uv, width, height, clip)
    tri = np.full(mask.shape, -1, np.int64)
    bary = np.zeros(mask.shape + (3,), np.float64)
    count = np.zeros(mask.shape, np.int64)
    if region.is_empty:
        return region, tri, bary, count
    flat_tri, flat_bary, flat_count = tri.reshape(-1), bary.reshape(-1, 3), count.reshape(-1)
    for t, xs, ys, l0, l1, l2 in covered(to_pixels(tri_uv, width, height), region):
        texel = (ys - region.y0) * region.width + (xs - region.x0)
        flat_count += np.bincount(texel, minlength=len(flat_count))
        texel, first = np.unique(texel, return_index=True)        # the lowest triangle of each texel here
        free = flat_tri[texel] < 0
        texel, first = texel[free], first[free]
        flat_tri[texel] = t[first]
        flat_bary[texel] = np.stack([l0[first], l1[first], l2[first]], axis=-1)
    return region, tri, bary, count


def dilate(mask, n):
    """Square (8-neighbour) dilation by n texels; the result is n texels larger on every side."""
    out = np.zeros((mask.shape[0] + 2 * n, mask.shape[1] + 2 * n), bool)
    out[n:n + mask.shape[0], n:n + mask.shape[1]] = mask
    for _ in range(n):
        grown = out.copy()
        grown[1:, :] |= out[:-1, :]
        grown[:-1, :] |= out[1:, :]
        grown[:, 1:] |= out[:, :-1]
        grown[:, :-1] |= out[:, 1:]
        grown[1:, 1:] |= out[:-1, :-1]
        grown[1:, :-1] |= out[:-1, 1:]
        grown[:-1, 1:] |= out[1:, :-1]
        grown[:-1, :-1] |= out[1:, 1:]
        out = grown
    return out


def grow(values, known, steps, fill=None):
    """Fill up to `steps` rings of texels around `known` from it: each new texel is the mean of its
    known 8-neighbours. Only `known` texels seed the result; other values are ignored. With `fill`, a
    mask, only its texels are filled, so the rings spread through them alone (reach). Returns
    (values as float64, filled mask), both the shape of the input.

    Each ring is computed at its own texels only, adding the 8 neighbours in a fixed order, so the cost
    follows the rings, not the area."""
    values = np.asarray(values)
    have = np.array(known, bool, copy=True)
    out = np.where(have.reshape(have.shape + (1,) * (values.ndim - 2)), values, np.float64(0.0))
    H, W = have.shape
    for _ in range(steps):
        frontier = _near(have) & ~have
        if fill is not None:
            frontier &= fill
        ys, xs = np.nonzero(frontier)
        if not len(ys):
            break
        acc = np.zeros((len(ys),) + out.shape[2:], np.float64)
        cnt = np.zeros(len(ys), np.float64)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if not (dx or dy):
                    continue
                sy, sx = ys - dy, xs - dx
                ok = (sy >= 0) & (sy < H) & (sx >= 0) & (sx < W)
                acc[ok] += out[sy[ok], sx[ok]]          # texels not known hold exactly 0: they add nothing
                cnt[ok] += have[sy[ok], sx[ok]]
        out[ys, xs] = acc / cnt[:, None] if out.ndim == 3 else acc / cnt
        have[ys, xs] = True
    return out, have


def _near(have):
    """`have` and its 8-neighbours."""
    near = have.copy()
    near[1:, :] |= have[:-1, :]
    near[:-1, :] |= have[1:, :]
    near[:, 1:] |= have[:, :-1]
    near[:, :-1] |= have[:, 1:]
    near[1:, 1:] |= have[:-1, :-1]
    near[1:, :-1] |= have[:-1, 1:]
    near[:-1, 1:] |= have[1:, :-1]
    near[:-1, :-1] |= have[1:, 1:]
    return near


def reach(seed, allowed, steps):
    """Texels reached from `seed` in up to `steps` 8-neighbour steps through `allowed` texels, `seed` left out:
    what grow(values, seed, steps, fill=allowed) fills. Same shape as `seed`."""
    have = np.array(seed, bool, copy=True)
    allowed = np.asarray(allowed, bool)
    for _ in range(steps):
        new = _near(have) & ~have & allowed
        if not new.any():
            break
        have |= new
    return have & ~np.asarray(seed, bool)


def bands(height, width, context):
    """Row bands (r0, r1, lo, hi) covering `height` rows of `width` texels, BAND texels each at most (one
    row at least): rows r0..r1 are the band's own, lo..hi the same with `context` rows on each side inside
    the array. A ring growth of up to `context` steps over lo..hi gives r0..r1 as over the whole array."""
    step = max(1, BAND // max(width, 1))
    for r0 in range(0, height, step):
        r1 = min(r0 + step, height)
        yield r0, r1, max(r0 - context, 0), min(r1 + context, height)


def is_integer(value, tol):
    return abs(value - round(value)) <= tol and math.isfinite(value)
