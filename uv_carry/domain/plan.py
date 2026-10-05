"""TransferPlan: what one carry reads, computes and writes in each image target, with no side effects.

One verified map takes the island from its origin UVs to its final UVs, and every target uses it at
its own resolution. A move alone keeps the exact whole-texel copy: the move is rounded to whole
texels of the finest target (the one with the most texels), which snaps the island by less than half
of one of its texels, and every target whose texels the rounded move also spans exactly is copied bit
for bit; any other target is resampled under that same move. Rotation and
scale resample every target. The memory estimate covers the frozen sources, the rollback
snapshots and the patches, and the largest working set of one target (its whole image, or the arrays
its computation takes); a plan over the budget is refused before any write (MemoryBudgetExceeded).

One Ctrl+Enter may carry several islands (CarryPlan). Each island is planned on its
own, with its own map and snap, and any island that cannot be carried refuses them all. Each image is published
once, over the union of the islands' write regions (compose). Islands that did not move are padded where they
are instead (uv_carry.domain.padding).
"""

from dataclasses import dataclass

import numpy as np

from .affine import REFUSALS, AffineFit, fit_affine
from .padding import apply_padding, plan_padding
from .raster import BAND, PixelRegion, cover, is_integer, tile
from .resample import EXTEND, apply_affine, plan_affine
from .translation import (PIXEL_TOL, TRANSLATION_TOL, TransferError, apply_translation, measure_translation,
                          plan_translation, to_whole_texels)

MIB = 2 ** 20
SLACK = 8 * MIB         # the session's own small objects, in the memory estimate


@dataclass(frozen=True)
class ImageTarget:
    key: str                # identity of the image
    width: int
    height: int
    channels: int
    itemsize: int = 4       # bytes per channel value in the copies the plan makes (float32)
    alpha: str = "independent"      # alpha policy (semantics.alpha_policy): "straight" computes a premultiplied pass too


@dataclass(frozen=True, eq=False)
class TargetPlan:
    target: ImageTarget
    method: str             # "copy" (whole texels, bit for bit), "resample" (bilinear, island texels only) or "pad"
    raster: object          # TranslationPlan for "copy", AffinePlan for "resample", PaddingPlan for "pad"

    @property
    def source(self):
        return self.raster.source

    @property
    def write(self):
        return self.raster.write

    @property
    def nbytes(self):
        """Frozen source, rollback snapshot and patch: what the carry keeps of this target until it is
        published."""
        c, s = self.target.channels, self.target.itemsize
        return self.raster.frozen_bytes(c, s) + 2 * self.raster.write_bytes(c, s)

    @property
    def mask_bytes(self):
        """The plan's own masks (one byte a texel), kept with it."""
        r = self.raster
        return r.source.area + 2 * r.write.area + (r.dest.area if self.method == "resample" else 0)

    @property
    def work_bytes(self):
        """An upper bound of the memory reading, computing and writing this target takes besides what is
        kept: the whole image as float32 (Blender moves pixels whole), or the arrays of the computation,
        whichever is larger. Bands (raster.BAND) bound the per-texel work; checked by tests/blender/measure.py."""
        t, r = self.target, self.raster
        c = t.channels
        band = min(BAND, r.write.area) * (56 * c + 64)
        if self.method in ("copy", "pad"):
            work = r.write.area * 4 * c + band
        else:
            work = r.source.area * (16 * c + 9) + r.interior_texels * 112 + r.write.area * 4 * c + band
        if t.alpha == "straight" and c >= 4:        # the premultiplied pass, over float64 copies
            work += (r.source.area + 3 * r.write.area) * 8 * c
        whole = t.width * t.height * c * 4 + max(r.source.area, r.write.area) * c     # and its comparisons
        return max(whole, work)


@dataclass(frozen=True, eq=False)
class TransferPlan:
    kind: str               # "translation" or "affine"
    fit: AffineFit          # the verified map, origin to final, in UV units
    snap_to: tuple          # (du, dv) whole move the island's UVs are set to after the write, or None
    snapped_px: float       # how far that snap moves the island, in texels of the grid target
    targets: tuple          # TargetPlan per image, in application order
    grid: str = None        # key of the target whose texels a move is rounded to; None for an affine plan

    @property
    def nbytes(self):
        """What the carry keeps of every target until it is published, the plan's masks, the largest
        working set of one target (targets are computed and written one at a time), and SLACK."""
        return (sum(t.nbytes + t.mask_bytes for t in self.targets)
                + max((t.work_bytes for t in self.targets), default=0) + SLACK)


@dataclass(frozen=True, eq=False)
class TargetCarry:
    """What one Ctrl+Enter publishes in one image: its parts, one TargetPlan per island of a carry or the one
    TargetPlan of a padding, composed over `region`, the union of their write regions."""

    target: ImageTarget
    region: object          # PixelRegion
    parts: tuple            # TargetPlan, in island order
    islands: tuple = ()     # the island of each part (empty for a padding, whose one part covers them all)

    @property
    def empty(self):
        """Nothing to write: a padding with no texel to fill."""
        return not any(p.raster.write_mask.any() for p in self.parts)


@dataclass(frozen=True, eq=False)
class CarryPlan:
    """One Ctrl+Enter over one or more islands: a TransferPlan per island, in island order, and a TargetCarry
    per image target, in application order. An island padded where it is has a TransferPlan of kind "still"
    that only says whether its UVs go back to its origin."""

    kind: str               # "translation", "affine" or "mixed" (the islands moved), or "padding"
    islands: tuple          # TransferPlan per island
    targets: tuple          # TargetCarry per image target
    margin: int = 0

    @property
    def grid(self):
        return next((p.grid for p in self.islands if p.grid), None)

    @property
    def snapped_px(self):
        return max((p.snapped_px for p in self.islands), default=0.0)

    @property
    def nbytes(self):
        """TransferPlan.nbytes over every part; with several parts in an image, also the union's rollback
        snapshot and patch, and the two masks compose() keeps. One island alone keeps its own estimate."""
        kept = 0
        for t in self.targets:
            kept += sum(p.nbytes + p.mask_bytes for p in t.parts)
            if len(t.parts) > 1:
                kept += t.region.area * (2 * t.target.channels * t.target.itemsize + 2)
        work = max((p.work_bytes for t in self.targets for p in t.parts), default=0)
        return kept + work + SLACK


def translation_fit(du, dv):
    return AffineFit(np.eye(2), np.array([du, dv], np.float64), 0.0, 1.0, np.ones(2), "ok")


def plan_transfer(origin_corners, final_corners, origin_triangles, final_triangles, targets, margin=0,
                  budget_bytes=None):
    """Plan the carry of an island into `targets` (ImageTarget, in application order). Corners are the
    island's corner UVs, (N, 2); triangles its UV triangles, (T, 3, 2), at the origin and now. Raises
    TransferError when the move cannot be carried or the plan exceeds `budget_bytes`. No writes."""
    targets = tuple(targets)
    _check_targets(targets)
    du, dv, deviation = measure_translation(origin_corners, final_corners)
    if deviation <= TRANSLATION_TOL:
        plan = _plan_translation(du, dv, np.asarray(origin_triangles, np.float64), targets, margin)
    else:
        fit = fit_affine(origin_corners, final_corners)
        if not fit.ok:
            raise TransferError("InvalidTransform", REFUSALS[fit.verdict], verdict=fit.verdict)
        plans = tuple(TargetPlan(t, "resample", plan_affine(origin_triangles, final_triangles, fit, t.width,
                                                             t.height, margin)) for t in targets)
        plan = TransferPlan("affine", fit, None, 0.0, plans)
    _check_budget(plan, budget_bytes)
    return plan


def _check_budget(plan, budget_bytes):
    if budget_bytes is not None and plan.nbytes > budget_bytes:
        raise TransferError("MemoryBudgetExceeded",
                            f"Carry needs {plan.nbytes / MIB:.1f} MB of memory (texel copies and working "
                            f"arrays); the budget is {budget_bytes / MIB:.0f} MB", needed=plan.nbytes,
                            budget=budget_bytes)


def _check_targets(targets):
    if not targets:
        raise TransferError("UnsupportedMapping", "No image to carry into")
    keys = [t.key for t in targets]
    if len(set(keys)) != len(keys):
        raise ValueError(f"image targets must be deduplicated: {keys}")


def plan_carry(islands, targets, margin=0, budget_bytes=None, others=(), island_targets=None):
    """Plan one Ctrl+Enter over one or more islands into `targets` (ImageTarget, in application order).
    `islands` lists, per island in island order, its corner UVs (N, 2) at the origin and now, and its UV
    triangles (T, 3, 2) at the origin and now. `others` holds the other UV triangles of the mesh now, whose
    texels a padding never writes: one array, or a dict giving them per target key.

    `island_targets` lists, per island, the keys of the targets it writes, all of them when None: an island
    writes the images its own materials use.

    Islands that moved are carried, each by plan_transfer under its own map; each image is published over
    the union of the write regions of the islands that write it. Islands that did not move by a whole texel
    of their finest target are padded where they started; any that are not exactly there go back to it after
    the write. Raises TransferError, before any write, when an island cannot be carried (the whole carry is
    refused), when some islands moved and others did not, or when the plan exceeds `budget_bytes`. No writes."""
    targets = tuple(targets)
    _check_targets(targets)
    islands = [tuple(np.asarray(a, np.float64) for a in island) for island in islands]
    if not islands:
        raise TransferError("SelectionError", "No island to carry")
    keys = {t.key for t in targets}
    wanted = [keys if island_targets is None else set(island_targets[k]) & keys for k in range(len(islands))]
    own = [tuple(t for t in targets if t.key in wanted[k]) for k in range(len(islands))]
    still = [_still(oc, fc, grid_target(own[k] or targets)) for k, (oc, fc, _, _) in enumerate(islands)]
    if all(s is not None for s in still):
        plan = _plan_padding(islands, still, targets, margin, others, grid_target(targets), wanted)
    elif any(s is not None for s in still):
        idle = [k + 1 for k, s in enumerate(still) if s is not None]
        raise TransferError("InvalidTransform", f"{len(idle)} of {len(islands)} islands have not moved by a whole "
                            "texel while the others moved; move them together", islands=idle)
    else:
        plans = []
        for k, (oc, fc, ot, ft) in enumerate(islands):
            if not own[k]:                  # its materials reach no image of the carry: nothing to write
                plans.append(TransferPlan("untouched", translation_fit(0.0, 0.0), None, 0.0, ()))
                continue
            try:
                plans.append(plan_transfer(oc, fc, ot, ft, own[k], margin))
            except TransferError as exc:
                if len(islands) == 1:
                    raise
                raise TransferError(exc.category, f"{exc} (island {k + 1} of {len(islands)})", island=k + 1,
                                    **exc.detail) from exc
        carries = []
        for t in targets:
            idx = tuple(k for k in range(len(islands)) if t.key in wanted[k])
            if idx:
                parts = tuple(next(tp for tp in plans[k].targets if tp.target.key == t.key) for k in idx)
                carries.append(TargetCarry(t, union(tp.write for tp in parts), parts, idx))
        kinds = {p.kind for p in plans} - {"untouched"}
        plan = CarryPlan(kinds.pop() if len(kinds) == 1 else "mixed", tuple(plans), tuple(carries), margin)
    _check_budget(plan, budget_bytes)
    return plan


def _still(origin_corners, final_corners, grid):
    """(snap_to, snapped_px) for an island that has not moved by a whole texel of `grid`, None otherwise.
    snap_to is None when the island is exactly where it started, (0.0, 0.0) when it goes back there."""
    du, dv, deviation = measure_translation(origin_corners, final_corners)
    if deviation > TRANSLATION_TOL:
        return None
    dx, dy, _, _, _ = to_whole_texels(du, dv, grid.width, grid.height)
    if dx or dy:
        return None
    if np.array_equal(origin_corners, final_corners):
        return None, 0.0
    return (0.0, 0.0), float(max(abs(du) * grid.width, abs(dv) * grid.height))


def _plan_padding(islands, still, targets, margin, others, grid, wanted):
    plans = tuple(TransferPlan("still", translation_fit(0.0, 0.0), snap, snapped, (), grid.key)
                  for snap, snapped in still)
    carries = []
    for t in targets:
        users = [k for k in range(len(islands)) if t.key in wanted[k]]
        if not users:
            continue
        origin_tri = np.concatenate([islands[k][2].reshape(-1, 3, 2) for k in users])
        used = others.get(t.key, ()) if isinstance(others, dict) else others
        tp = TargetPlan(t, "pad", plan_padding(origin_tri, np.asarray(used, np.float64).reshape(-1, 3, 2),
                                               t.width, t.height, margin))
        carries.append(TargetCarry(t, tp.write, (tp,)))
    return CarryPlan("padding", plans, tuple(carries), margin)


def union(regions):
    """The smallest PixelRegion holding every region given."""
    regions = list(regions)
    return PixelRegion(min(r.x0 for r in regions), min(r.y0 for r in regions),
                       max(r.x1 for r in regions), max(r.y1 for r in regions))


def compose(region, before, parts):
    """Texels to publish over `region`: `before`, the texels there now, with each part's patch, `parts` being
    (TargetPlan, patch over its write region) in island order. Every margin is written first, then every
    interior, so an island's interior wins over any margin, and a later island's interior over an earlier
    one's. One part over the whole region is its own patch. Returns (texels, how many interior texels a later
    island wrote over an earlier one's with other values). Islands stacked on the same texels with the same
    values, as mirrored halves that share their texels are, count for nothing."""
    parts = list(parts)
    if len(parts) == 1 and parts[0][0].write == region:
        return parts[0][1], 0
    after = np.array(before, copy=True)
    seen = np.zeros((region.height, region.width), bool)
    twice = np.zeros_like(seen)
    for tp, patch in parts:
        ring = tp.raster.margin_mask
        after[tp.write.slices(region)][ring] = np.asarray(patch)[ring]
    for tp, patch in parts:
        inside = tp.raster.write_mask & ~tp.raster.margin_mask
        window = tp.write.slices(region)
        values = np.asarray(patch)[inside]
        view = after[window]
        clash = seen[window][inside]
        if clash.any():
            differ = (view[inside] != values).reshape(len(values), -1).any(axis=1)
            mark = twice[window]
            mark[inside] |= clash & differ
        view[inside] = values
        seen[window] |= inside
    return after, int(twice.sum())


def grid_target(targets):
    """The finest target, which sets the whole-texel grid of a move: the most texels, the first on a tie."""
    return max(targets, key=lambda t: t.width * t.height)


def _plan_translation(du, dv, origin_triangles, targets, margin):
    first = grid_target(targets)
    dx, dy, du_whole, dv_whole, whole = to_whole_texels(du, dv, first.width, first.height)
    if dx == 0 and dy == 0:
        raise TransferError("InvalidTransform", "Island has not moved by a whole texel")
    fit = translation_fit(du_whole, dv_whole)
    final_triangles = origin_triangles + np.array([du_whole, dv_whole])
    plans = []
    for t in targets:
        fx, fy = du_whole * t.width, dv_whole * t.height
        if is_integer(fx, PIXEL_TOL) and is_integer(fy, PIXEL_TOL):
            plans.append(TargetPlan(t, "copy", plan_translation(origin_triangles, t.width, t.height,
                                                                 int(round(fx)), int(round(fy)), margin)))
        else:
            plans.append(TargetPlan(t, "resample", plan_affine(origin_triangles, final_triangles, fit, t.width,
                                                                t.height, margin)))
    snapped = 0.0 if whole else float(max(abs(du_whole - du) * first.width, abs(dv_whole - dv) * first.height))
    return TransferPlan("translation", fit, None if whole else (du_whole, dv_whole), snapped, tuple(plans), first.key)


def snapshot_region(origin_triangles, width, height):
    """Texels to freeze when a session opens, for a target of this size: the island at its origin
    grown by the resampling taps, inside the tile. Holds the source of any plan of that island. An island
    thinner than a texel freezes the texel under its centroid (raster.cover)."""
    region, _ = cover(origin_triangles, width, height)
    if region.is_empty:
        raise TransferError("SelectionError", f"Island has no UV triangle to freeze at {width}x{height}")
    return region.expand(EXTEND).intersect(tile(width, height))


def compute_patch(target_plan, frozen, before, alpha="independent"):
    """(texels to publish over target_plan.write, samples that fell back to the nearest island texel),
    from the frozen source over target_plan.source and the texels now in target_plan.write. A padding
    reads only `before`, which holds the islands; its `frozen` is not used.

    With alpha "straight" (uv_carry.domain.semantics.alpha_policy), every texel the carry computes
    rather than copies, resampled texels and the margin, is interpolated with premultiplied colour and
    then divided back, so transparent texels lend no colour to their neighbours. Copied texels stay bit
    for bit. Where the computed alpha is 0, the colour interpolated channel by channel is kept."""
    method = target_plan.method
    if method == "copy":
        patch, fallback = apply_translation(target_plan.raster, frozen, before), 0
    elif method == "pad":
        patch, fallback = apply_padding(target_plan.raster, before), 0
    else:
        patch, fallback = apply_affine(target_plan.raster, frozen, before)
    if alpha != "straight" or np.shape(before)[-1] < 4:
        return patch, fallback
    raster = target_plan.raster
    computed = raster.write_mask if method == "resample" else raster.margin_mask
    if not computed.any():
        return patch, fallback
    if method == "copy":
        premul = apply_translation(raster, premultiply(frozen), premultiply(before))
    elif method == "pad":
        premul = apply_padding(raster, premultiply(before))
    else:
        premul, _ = apply_affine(raster, premultiply(frozen), premultiply(before))
    out = np.array(patch, copy=True)
    out[computed] = unpremultiply(premul[computed], patch[computed]).astype(out.dtype)
    return out, fallback


def premultiply(texels):
    out = np.array(texels, np.float64, copy=True)
    out[..., :3] *= out[..., 3:4]
    return out


def unpremultiply(premul, straight):
    """Straight colour from premultiplied texels; where alpha is 0, the straight texels' colour."""
    out = np.array(premul, np.float64, copy=True)
    a = out[..., 3]
    solid = a > 0
    out[solid, :3] /= a[solid, None]
    out[~solid, :3] = np.asarray(straight, np.float64)[~solid, :3]
    return out
