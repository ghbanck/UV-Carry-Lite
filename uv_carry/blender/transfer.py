"""Blender adapter: the selected islands' texels follow them to where they were moved, in one or more images.

The domain plans the carry (uv_carry.domain.plan): an exact whole-texel copy for a move alone and
a resampling under one verified affine map for rotation and scale, each island under its own map
and each image at its own resolution, or the padding of islands that stay where they are. Each island carries the images of its own materials. This adapter freezes the texels under
each island when a session opens, reads what the plan needs from the images and the edit mesh, computes
every patch before the first write, and publishes them with the islands' UVs through the in-memory
transaction (uv_carry.domain.transaction). Call only when no other modal operator runs.
Every check that can refuse the carry runs before an image
is written.
"""

from dataclasses import dataclass, replace

import bpy
import numpy as np

from ..domain import transaction
from ..domain.islands import FaceCorners, classify_selection, resolve_islands
from ..domain.plan import ImageTarget, compose, compute_patch, plan_carry, snapshot_region
from ..domain.raster import rasterize
from ..domain.translation import TRANSLATION_TOL, TransferError, measure_translation
from . import image_io, mesh_uv


@dataclass(frozen=True, eq=False)
class IslandOrigin:
    """The selected islands as they were when their session started."""

    uv_map: str
    topology: str
    corners: np.ndarray        # corner indices of every island, in corner order
    faces: np.ndarray          # face indices of every island
    triangles: np.ndarray      # (T, 3) triangles of every island, corner indices
    uv: np.ndarray             # UV of every corner at the origin
    corner_island: np.ndarray  # per entry of `corners`: its island, 0, 1, ... in island order
    triangle_island: np.ndarray    # per row of `triangles`: its island
    face_island: np.ndarray = None     # per entry of `faces`: its island

    @property
    def count(self):
        """How many islands."""
        return int(self.corner_island.max()) + 1 if len(self.corner_island) else 0

    def island(self, k):
        """(corner indices, rows of `triangles`) of island k."""
        return self.corners[self.corner_island == k], np.flatnonzero(self.triangle_island == k)

    def island_faces(self, k):
        """Face indices of island k."""
        return self.faces[self.face_island == k]


@dataclass(frozen=True, eq=False)
class Frozen:
    """Texels of one image under the islands that read it, at their origin, read when the session opened.
    A padding without a session freezes no texels: it keeps the image's identity and fingerprint only."""

    target: ImageTarget
    regions: tuple             # PixelRegion per island, in island order; None for an island that does not read it
    texels: tuple              # the texels of each region, None alike
    fingerprint: dict          # the image as it was then


@dataclass(frozen=True, eq=False)
class Prepared:
    """A planned carry with every patch computed; nothing written yet."""

    plan: object               # CarryPlan
    patches: list              # ImagePatch per target that has texels to write, in plan order
    reports: list              # one dict per target
    placed: np.ndarray         # the islands' corner UVs before the carry


def select_islands(mesh, uv=None):
    """(Selection, IslandOrigin or None) for the UV selection of `mesh`, an EditMeshUV: one or more complete
    islands.

    Islands are resolved over the faces shown in the UV editor, on `uv` when given (every corner's UV
    before a transform) and on the mesh's own UVs otherwise."""
    return _select(mesh, uv, several=True)


def select_island(mesh, uv=None):
    """select_islands() for exactly one island: several are a "multiple" selection."""
    return _select(mesh, uv, several=False)


def _select(mesh, uv, several):
    corners = mesh.corners
    if uv is not None:
        corners = FaceCorners(corners.face_start, corners.face_len, corners.corner_vert,
                              np.asarray(uv, np.float64).reshape(-1, 2))
    ids = resolve_islands(corners, face_mask=mesh.visible)
    corner_face = corners.corner_face
    selection = classify_selection(ids, corner_face, mesh.selected, several)
    if selection.kind != "ok":
        return selection, None
    chosen = np.asarray(selection.islands)
    of_corner = ids[corner_face]
    picked = np.flatnonzero(np.isin(of_corner, chosen))
    faces = np.flatnonzero(np.isin(ids, chosen))
    rows = np.isin(mesh.triangle_face, faces)
    return selection, IslandOrigin(mesh.uv_map, mesh.topology, picked, faces, mesh.triangles[rows], corners.uv.copy(),
                                   np.searchsorted(chosen, of_corner[picked]),
                                   np.searchsorted(chosen, ids[mesh.triangle_face[rows]]),
                                   np.searchsorted(chosen, ids[faces]))


def island_slots(mesh, origin):
    """The material slots of each island's faces, in island order."""
    face_slot = mesh.face_material if mesh.face_material is not None else np.zeros(len(mesh.corners.face_start), int)
    return [sorted({int(s) for s in face_slot[origin.island_faces(k)]}) for k in range(origin.count)]


def island_images(mesh, origin, slot_images, everyone=()):
    """The image keys each island reads: those of its own materials (slot_images, {slot: keys}), and
    `everyone`, images every island reads (the UV editor's own image)."""
    return [set().union(*(slot_images.get(s, set()) for s in slots), everyone)
            for slots in island_slots(mesh, origin)]


def image_target(img):
    width, height, channels = image_io.size(img)
    return ImageTarget(img.name, width, height, channels)


def image_fingerprint(img):
    """What must not change in an image between the session opening and the carry."""
    width, height, channels = image_io.size(img)
    return {"pointer": img.as_pointer(), "size": [width, height], "channels": channels, "is_float": bool(img.is_float),
            "source": img.source, "colorspace": img.colorspace_settings.name, "alpha_mode": img.alpha_mode}


def _loaded(img):
    target = image_target(img)
    if not (target.width and target.height):
        raise TransferError("UnsupportedMapping", f"Image {img.name} has no pixels loaded")
    return target


def snapshot_regions(img, origin, readers=None):
    """The region of `img` to freeze under each island at `origin` that reads it (domain.plan.snapshot_region),
    None under the others; `readers` holds the islands that read it, every island when None. Reads no texel."""
    target = _loaded(img)
    return tuple(snapshot_region(origin.uv[origin.triangles[origin.island(k)[1]]], target.width, target.height)
                 if readers is None or k in readers else None for k in range(origin.count))


def frozen_bytes(img, regions):
    """Bytes the frozen texels of `regions` take in `img` (float32)."""
    return sum(r.area for r in regions if r is not None) * image_io.size(img)[2] * 4


def freeze(img, origin, regions=None):
    """Frozen texels of `img` under each island at `origin` that reads it, the image read once; `regions` from
    snapshot_regions() when already known, every island otherwise."""
    regions = snapshot_regions(img, origin) if regions is None else regions
    whole = image_io.pixels(img)
    return Frozen(image_target(img), regions,
                  tuple(None if r is None else whole[r.y0:r.y1, r.x0:r.x1].copy() for r in regions),
                  image_fingerprint(img))


def watch(img):
    """A Frozen with no texels, for a padding without a session: the image's identity and fingerprint."""
    return Frozen(_loaded(img), (), (), image_fingerprint(img))


def image_changes(frozen, images=None, texels=False):
    """Labels of what changed in the frozen images since they were frozen: gone, replaced, resized or
    another format, and with `texels`, other texels under the islands. Empty when nothing did."""
    out = []
    for fr in frozen:
        img = (images or {}).get(fr.target.key) or bpy.data.images.get(fr.target.key)
        if img is None:
            out.append(f"image {fr.target.key} removed")
        elif image_fingerprint(img) != fr.fingerprint:
            out.append(f"image {fr.target.key} replaced or resized")
        elif texels and edited(fr, image_io.pixels(img)):
            out.append(f"image {fr.target.key} edited under the island")
    return out


def edited(fr, whole):
    """Whether `whole`, every texel of the image of `fr`, differs from the frozen texels under any island."""
    return any(r is not None and not np.array_equal(whole[r.y0:r.y1, r.x0:r.x1], t)
               for r, t in zip(fr.regions, fr.texels))


def _frozen_part(fr, k, tp):
    """The frozen texels of island k in the image of `fr` over the source region of `tp`."""
    if not fr.regions or fr.regions[k] is None:
        raise TransferError("ContextChanged", f"Image {fr.target.key} was not frozen under island {k + 1}")
    return fr.texels[k][tp.source.slices(fr.regions[k])]


def _d1_texels(others, write, write_mask, width, height):
    """Write-mask texels that the UV triangles `others` ((T, 3, 2), at their current position) use."""
    if not len(others):
        return 0
    region, mask = rasterize(others, width, height, clip=write)
    return 0 if region.is_empty else int((write_mask[region.slices(write)] & mask).sum())


def _over(region, parts, mask):
    """The union over `region` of mask(raster) of each part, placed at the part's write region."""
    out = np.zeros((region.height, region.width), bool)
    for tp in parts:
        out[tp.write.slices(region)] |= mask(tp.raster)
    return out


def _numbers(target_plan, plan, fallback):
    """What one island's part of an image did: its move or its map, and its texels."""
    r = target_plan.raster
    out = {"method": target_plan.method}
    if target_plan.method == "copy":
        out.update({"dx": r.dx, "dy": r.dy, "snapped_px": plan.snapped_px})
    else:
        out.update({"rotation_deg": plan.fit.rotation_deg, "scale": [float(s) for s in plan.fit.singular_values],
                    "residual_uv": plan.fit.residual_max, "fallback_texels": fallback,
                    "filter": "bilinear, island texels only"})
        if plan.kind == "translation":
            out["snapped_px"] = plan.snapped_px
    return out


def _report(target_plan, plan, fallback, d1, margin):
    """Report of one island in one image."""
    t, r = target_plan.target, target_plan.raster
    out = {"image": t.key, "size": [t.width, t.height, t.channels], "method": target_plan.method, "margin": margin}
    out.update(_numbers(target_plan, plan, fallback))
    out.update({"interior_texels": r.interior_texels, "margin_texels": int(r.margin_mask.sum()),
                "overlap_texels": r.overlap_texels, "d1_texels": d1, "source": r.source.__dict__,
                "write": r.write.__dict__, "frozen_bytes": r.frozen_bytes(t.channels),
                "write_bytes": r.write_bytes(t.channels), "image_bytes": t.width * t.height * t.channels * 4,
                "coverage_rule": r.coverage_rule})
    return out


def _report_islands(carry, plan, fallbacks, d1, overlap, margin):
    """Report of several islands in one image, composed over carry.region."""
    t, region = carry.target, carry.region
    interiors = _over(region, carry.parts, lambda r: r.write_mask & ~r.margin_mask)
    margins = _over(region, carry.parts, lambda r: r.margin_mask) & ~interiors
    per = [dict(_numbers(tp, plan.islands[k], fb), island=k + 1, interior_texels=tp.raster.interior_texels,
                write=tp.write.__dict__) for k, tp, fb in zip(carry.islands, carry.parts, fallbacks)]
    methods = sorted({n["method"] for n in per})
    return {"image": t.key, "size": [t.width, t.height, t.channels], "method": " and ".join(methods),
            "margin": margin, "islands": len(per), "interior_texels": int(interiors.sum()),
            "margin_texels": int(margins.sum()), "islands_overlap_texels": overlap, "d1_texels": d1,
            "fallback_texels": int(sum(fallbacks)), "snapped_px": plan.snapped_px, "write": region.__dict__,
            "frozen_bytes": sum(tp.raster.frozen_bytes(t.channels) for tp in carry.parts),
            "write_bytes": region.area * t.channels * 4, "image_bytes": t.width * t.height * t.channels * 4,
            "coverage_rule": carry.parts[0].raster.coverage_rule, "per_island": per}


def _report_padding(carry, plan, d1, margin):
    """Report of a padding in one image."""
    t, (tp,) = carry.target, carry.parts
    r = tp.raster
    return {"image": t.key, "size": [t.width, t.height, t.channels], "method": "pad", "margin": margin,
            "islands": len(plan.islands), "padded_texels": r.padded_texels, "interior_texels": 0,
            "margin_texels": r.padded_texels, "d1_texels": d1, "snapped_px": plan.snapped_px,
            "write": r.write.__dict__, "write_bytes": r.write_bytes(t.channels),
            "image_bytes": t.width * t.height * t.channels * 4, "coverage_rule": r.coverage_rule}


def _others(mesh, origin, final, targets, slot_images):
    """{target key: UV triangles of the faces outside the islands that use that image, now}: what a padding
    never writes and what D1 counts."""
    face_slot = mesh.face_material if mesh.face_material is not None else np.zeros(len(mesh.corners.face_start), int)
    outside = ~np.isin(mesh.triangle_face, origin.faces)
    out = {}
    for t in targets:
        if slot_images is None or not any(t.key in keys for keys in slot_images.values()):
            users = np.ones(len(face_slot), bool)
        else:
            users = np.isin(face_slot, [s for s, keys in slot_images.items() if t.key in keys])
        out[t.key] = final[mesh.triangles[outside & users[mesh.triangle_face]]]
    return out


def prepare(obj, origin, images, frozen, margin, budget_bytes=None, alphas=None, slot_images=None,
            island_targets=None):
    """Plan the carry of the islands from `origin` to where they are now and compute every patch
    (uv_carry.domain.plan.plan_carry). Each island writes the frozen images it reads, in the order given:
    `island_targets` lists them per island, and otherwise those frozen under it (every island, for an image
    frozen under none, as a padding's).

    `images` maps image names to images, `alphas` image names to their alpha policy
    (uv_carry.domain.semantics.alpha_policy; channel by channel when absent), and `slot_images` material slots to the images their material uses (every face uses every image when None).
    Each image written is read once, whole: its texels under the islands must still be the frozen ones, and
    its texels under the region the carry publishes become the rollback snapshot. A padding target with
    nothing to fill is reported and not read. Raises TransferError before any write when the carry cannot
    be made, and transaction.ContextChanged when an image was edited under an island since it was frozen."""
    mesh = mesh_uv.read(obj, origin.uv_map)
    if mesh.topology != origin.topology:
        raise TransferError("ContextChanged", "Topology changed since the island's origin")
    final = mesh.corners.uv
    alphas = alphas or {}
    by_key = {f.target.key: f for f in frozen}
    islands = [origin.island(k) for k in range(origin.count)]
    wanted = [set(island_targets[k]) for k in range(len(islands))] if island_targets is not None else \
        [{f.target.key for f in frozen if not f.regions or f.regions[k] is not None} for k in range(len(islands))]
    targets = [replace(f.target, alpha=alphas.get(f.target.key, "independent")) for f in frozen]
    others = _others(mesh, origin, final, targets, slot_images)
    plan = plan_carry([(origin.uv[c], final[c], origin.uv[origin.triangles[rows]], final[origin.triangles[rows]])
                       for c, rows in islands], targets, margin, budget_bytes, others, wanted)
    patches, reports = [], []
    for carry in plan.targets:
        key, region, target = carry.target.key, carry.region, carry.target
        if carry.empty:
            reports.append(_report_padding(carry, plan, 0, margin))
            continue
        whole = image_io.pixels(images[key])
        if edited(by_key[key], whole):
            raise transaction.ContextChanged([f"image {key} edited under the island"])
        before = whole[region.y0:region.y1, region.x0:region.x1].copy()
        del whole
        parts, fallbacks = [], []
        for k, tp in zip(carry.islands, carry.parts) if carry.islands else ((None, carry.parts[0]),):
            window = tp.write.slices(region)
            source = None if tp.method == "pad" else _frozen_part(by_key[key], k, tp)
            patch, fallback = compute_patch(tp, source, before[window], alphas.get(key, "independent"))
            parts.append((tp, patch))
            fallbacks.append(fallback)
        after, overlap = compose(region, before, parts)
        del parts
        written = _over(region, carry.parts, lambda r: r.write_mask)
        d1 = _d1_texels(others[key], region, written, target.width, target.height)
        patches.append(transaction.ImagePatch(key, region, before, after))
        if plan.kind == "padding":
            reports.append(_report_padding(carry, plan, d1, margin))
        elif len(carry.parts) == 1:
            reports.append(_report(carry.parts[0], plan.islands[carry.islands[0]], fallbacks[0], d1, margin))
        else:
            reports.append(_report_islands(carry, plan, fallbacks, d1, overlap, margin))
    return Prepared(plan, patches, reports, final[origin.corners].copy())


def publish(obj, origin, images, prepared, revalidate=None, faults=None, frozen=None):
    """Write the prepared patches, then put the islands' UVs on their whole moves (or back at their origin)
    when the plan says so, through the in-memory transaction. Returns its OperationResult.

    Before each image write, `revalidate(i)` checks the context, and the image is read whole: its texels
    under the write region must still be the rollback snapshot, and under the islands, with `frozen` (the
    Frozen of each patch), the frozen texels. The write patches that same copy."""
    held = {}

    def check(i):
        changed = revalidate(i) if revalidate is not None else []
        if changed:
            return changed
        patch = prepared.patches[i]
        whole = image_io.pixels(images[patch.target])
        r = patch.region
        if not np.array_equal(whole[r.y0:r.y1, r.x0:r.x1], patch.before) or (
                frozen is not None and edited(frozen[i], whole)):
            return [f"image {patch.target} edited under the island"]
        held[patch.target] = whole
        return []

    def write_image(key, region, texels):
        whole = held.pop(key, None)
        if whole is None:                   # rollbacks read the image afresh
            image_io.write_region(images[key], region, texels)
            return
        whole[region.y0:region.y1, region.x0:region.x1] = texels
        image_io.store(images[key], whole)

    write_uvs = restore_uvs = None
    snaps = [k for k, p in enumerate(prepared.plan.islands) if p.snap_to is not None]
    if snaps:
        corners = np.concatenate([origin.island(k)[0] for k in snaps])
        snapped = np.concatenate([origin.uv[origin.island(k)[0]] + np.asarray(prepared.plan.islands[k].snap_to)
                                  for k in snaps])

        def write_uvs():
            mesh_uv.write_uvs(obj, origin.uv_map, corners, snapped)

        def restore_uvs():
            mesh_uv.write_uvs(obj, origin.uv_map, origin.corners, prepared.placed)
    return transaction.publish(prepared.patches, write_image, write_uvs, restore_uvs, check, faults)


def carry(obj, img, origin, margin, undo=None):
    """Carry the island's texels from `origin` to where the island is now, in one image, reading its
    frozen source now. Returns the image's report. Raises TransferError before any write when the carry
    cannot be made, and RuntimeError when publishing failed (rolled back). With `undo`, a list, appends
    the write as (region, texels before, texels after)."""
    images = {img.name: img}
    prepared = prepare(obj, origin, images, [freeze(img, origin)], margin)
    result = publish(obj, origin, images, prepared)
    if not result.ok:
        raise RuntimeError(f"{result.category}: {result.error}; rollback {result.rollback}")
    if undo is not None:
        undo.extend((p.region, p.before, p.after) for p in prepared.patches)
    return prepared.reports[0]


def carry_translation(obj, img, origin, margin, undo=None):
    """carry() for a move alone; raises TransferError, before any write, when the island was also
    rotated, scaled or reshaped."""
    mesh = mesh_uv.read(obj, origin.uv_map)
    deviation = measure_translation(origin.uv[origin.corners], mesh.corners.uv[origin.corners])[2]
    if deviation > TRANSLATION_TOL:
        raise TransferError("InvalidTransform", "Island was rotated, scaled or reshaped, not only moved",
                            deviation=deviation)
    return carry(obj, img, origin, margin, undo)


def summary(report):
    """One line describing the carry report of one island in one image."""
    if "rotation_deg" in report:
        s0, s1 = report["scale"]
        scale = f"{s0:.2f}" if abs(s0 - s1) < 0.005 else f"{s0:.2f} x {s1:.2f}"
        msg = (f"Carried {report['interior_texels']} texels, rotated {report['rotation_deg']:.1f} deg and scaled "
               f"{scale} (bilinear), into {report['image']}")
    else:
        msg = f"Carried {report['interior_texels']} texels by ({report['dx']}, {report['dy']}) px into {report['image']}"
    if report["margin_texels"]:
        msg += f", margin {report['margin_texels']}"
    if report["d1_texels"]:
        msg += f"; {report['d1_texels']} texels of other UVs overwritten"
    if report.get("snapped_px"):
        msg += f"; island snapped {report['snapped_px']:.2f} px to whole texels"
    return msg
