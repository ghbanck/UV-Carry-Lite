"""UV Carry tool mode: while the tool is on, moving one or more complete UV islands opens a session by itself,
and Ctrl+Enter over the UV editor carries the islands' texture to where the islands now are.

  * native G/R/S stay native; Esc cancels only the running transform; Ctrl+Enter during a transform
    only confirms it (the transform consumes it); Ctrl+Enter with no transform running commits;
  * a session's origin is the islands before their first move, with the texels under each frozen in
    every image target;
  * a change of object, mode, mesh, active UV map, topology, vertex positions, UV selection, of any UV
    outside the islands or of an image target during a session invalidates it and puts the islands back
    where the session started;
  * the carry publishes every image and the islands' UVs together, or rolls back what it wrote;
  * Cancel (header or sidebar) and turning the tool off put the islands back too;
  * Ctrl+Enter with complete islands selected and no session pads them where they are, and so does a
    session whose islands are back where it started.

Origin capture under the adapter rule (no edit-mesh reads while another modal operator runs, or while a job
holds the interface, as UV > Pack Islands does while it packs): a key or button press over the UV editor,
when nothing else runs, reads the edit mesh before Blender handles the press. When a transform is then
confirmed, that read is the state before the move. It becomes the session origin only if, after the move,
complete islands are selected, vertex positions, topology and shown faces are unchanged, and every UV
outside the islands is exactly where it was.
"""

import time
import traceback

import bpy
import numpy as np

from ..domain.affine import fit_affine
from ..domain.semantics import resolve
from ..domain.session import ALLOWED_TRANSFORMS, TransferSession
from ..domain.transaction import ContextChanged, Faults
from ..domain.translation import TRANSLATION_TOL, TransferError, measure_translation
from . import context as ctx
from . import evidence, imageset, mesh_uv, persist, transfer
from . import undo as ledger

POLL_INTERVAL = 0.03        # seconds between timer ticks
CHECK_INTERVAL = 0.5        # an idle session re-checks its context at least this often without input
COMMIT_KEYS = {"RET", "NUMPAD_ENTER"}
TRACED_KEYS = {"G", "R", "S", "ESC", "RET", "NUMPAD_ENTER", "TAB", "A", "Z"}
MODIFIER_KEYS = {"LEFT_CTRL", "RIGHT_CTRL", "LEFT_SHIFT", "RIGHT_SHIFT", "LEFT_ALT", "RIGHT_ALT", "OSKEY", "HYPER"}
NAVIGATION = {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE", "WHEELINMOUSE", "WHEELOUTMOUSE",
              "TRACKPADPAN", "TRACKPADZOOM", "MOUSEROTATE", "MOUSESMARTZOOM"}
# Operators that may run between the read before a move and the move itself without changing UVs.
QUIET_OPS = ("_OT_select", "_OT_view", "WM_OT_tool_set", "UV_CARRY_OT_image_choice")

MULTI_OBJECT = ("{n} objects are in Edit Mode: UV Carry carries one object at a time, and a move now would move the "
                "UVs of all {n} without their texture. Leave Edit Mode (Tab), select only {name}, and press Tab again")


def editing_meshes(win):
    """The mesh objects in edit mode in the window's view layer. Blender's own G, R, S and Pack Islands move
    the UVs of all of them; UV Carry follows one."""
    return [o for o in win.view_layer.objects if o.type == "MESH" and o.mode == "EDIT"]


CHANGE_LABELS = {"active_object": "active object", "uv_map_active": "active UV map", "geometry": "vertex positions",
                 "selection": "UV selection", "uvs_outside_island": "UVs outside the islands", "images": "image targets",
                 "materials": "materials"}
SELECTION_NOTES = {
    "empty": "Move not tracked: no UV selected",
    "partial": "Move not tracked: only part of an island selected; select whole islands (L over each) "
               "to carry their texture",
}
PADDING_NOTES = {
    "empty": "Nothing to carry or pad: select complete UV islands, then move them with G, R or S, or press "
             "Ctrl+Enter to pad them where they are",
    "partial": "Not padded: only part of an island selected; select whole islands (L over each)",
}

TOOL = None                 # the running tool, while on
FAULTS = Faults()           # failure injection for tests; nothing armed in normal use
SETTINGS = {"memory_budget_mb": 4096, "undo_budget_mb": 512}     # replaced from the add-on preferences
_counter = [0]


def editor_image(area):
    """The still image shown in a UV editor, if any: a candidate the user may include."""
    space = area.spaces.active if area is not None else None
    img = space.image if space is not None and space.type == "IMAGE_EDITOR" else None
    return img


def resolve_images(obj, mesh, origin, scene, area=None):
    """(ImageSet, facts, {key: image}) for the islands at `origin`: what their materials reach, plus the image
    shown in the UV editor as a candidate, with the scene's choices."""
    return imageset.image_set(obj, mesh, origin.faces, origin.uv_map, scene, editor_image(area))


def material_signature(obj, mesh, faces, uv_map, scene):
    """What the islands' materials make of their images, for the session fingerprint. The UV editor's
    image is left out: showing another image there does not change the materials."""
    return imageset.image_set(obj, mesh, faces, uv_map, scene)[0].signature


HOW = {"copy": "copied", "resample": "resampled", "copy and resample": "copied and resampled"}


def summarize(reports, plan):
    """One message for a committed carry or padding."""
    if plan.kind == "padding":
        return summarize_padding(reports, plan)
    several = len(plan.islands) > 1
    if len(reports) == 1 and not several:
        return transfer.summary(reports[0])
    if len(reports) == 1:
        r = reports[0]
        msg = f"Carried {len(plan.islands)} islands into {r['image']}: {r['interior_texels']} texels {HOW[r['method']]}"
        if r["margin_texels"]:
            msg += f", margin {r['margin_texels']}"
        if r["d1_texels"]:
            msg += f"; {r['d1_texels']} texels of other UVs overwritten"
    else:
        parts = []
        for r in reports:
            d1 = f", {r['d1_texels']} over other UVs" if r["d1_texels"] else ""
            parts.append(f"{r['image']} ({r['interior_texels']} texels {HOW[r['method']]}{d1})")
        what = f"{len(plan.islands)} islands" if several else "the island"
        msg = f"Carried {what} into {len(reports)} images: " + ", ".join(parts)
    landed = next((r for r in reports if r.get("islands_overlap_texels")), None)
    if landed is not None:
        msg += (f"; islands landed on each other over {landed['islands_overlap_texels']} texels of {landed['image']}, "
                f"where the last island's texels were kept")
    if plan.snapped_px:
        msg += f"; {'islands' if several else 'island'} snapped {plan.snapped_px:.2f} px to whole texels of {plan.grid}"
    return msg


def island_reads(obj, mesh, origin, image_set):
    """(the image keys each island reads, {material slot: image keys}): an island reads the images of its own
    materials, and every island reads an image in the set that no material reaches (the UV editor's)."""
    slots = imageset.slot_images(obj, mesh, origin.uv_map)
    everyone = {e.key for e in image_set.entries} - set().union(*slots.values())
    return transfer.island_images(mesh, origin, slots, everyone), slots


def summarize_padding(reports, plan):
    """One message for a padding: the texels filled in each image, or why there were none."""
    n = len(plan.islands)
    what = "the island" if n == 1 else f"{n} islands"
    padded = [(r["image"], r["padded_texels"]) for r in reports]
    total = sum(t for _, t in padded)
    if not total:
        why = "Margin (px) is 0" if not plan.margin else \
            f"every texel within {plan.margin} px of {what} is used by a UV"
        msg = f"Nothing to pad: {why}"
    elif len(padded) == 1:
        msg = f"Padded {what} by {plan.margin} px: {total} texels of {padded[0][0]} that no UV uses"
    else:
        msg = (f"Padded {what} by {plan.margin} px in {len(padded)} images, only texels no UV uses: "
               + ", ".join(f"{k} ({t} texels)" for k, t in padded))
    if plan.snapped_px:
        msg += f"; {'island' if n == 1 else 'islands'} put back where the session started ({plan.snapped_px:.2f} px)"
    return msg


def describe(changed):
    """Fingerprint keys and image changes as words: "UV selection changed", "image a.png edited under the
    island"."""
    return "; ".join(c if c.startswith("image ") else f"{CHANGE_LABELS.get(c, c)} changed" for c in changed)


# ---------------------------------------------------------------- session

class Snapshot:
    """The edit mesh as read before Blender handled a press, or at an idle moment."""

    def __init__(self, obj, mesh, tail):
        self.object_name, self.object_ptr, self.mesh_ptr = obj.name, obj.as_pointer(), obj.data.as_pointer()
        self.mesh = mesh
        self.tail = tail


class Runner:
    """The Blender side of one TransferSession: watches the context and carries out the session's
    commands on the mesh and the images. A padding (trigger "padding") is a session that commits as soon as
    it opens, from Ctrl+Enter with no move; it never stays open."""

    def __init__(self, tool, window, obj, origin, mesh, opening_ops, found, frozen, trigger="first_transform",
                 reads=None, slot_images=None):
        _counter[0] += 1
        self.tool = tool
        self.window_ptr = window.as_pointer()
        self.object_name, self.object_ptr = obj.name, obj.as_pointer()
        self.padding = trigger == "padding"
        self.trigger = trigger
        image_set, facts, _ = found
        self.facts = tuple(facts)                       # what the materials said when the session opened
        self.frozen = dict(frozen)                      # key -> transfer.Frozen, every image a carry may read
        self.reads = reads                              # per island: the image keys it reads (its materials')
        self.slot_images = slot_images                  # material slot -> image keys its material uses
        self.carried = []                               # the images the commit wrote
        self.outside = np.ones(len(mesh.corners.uv), bool)
        self.outside[origin.corners] = False
        self.origin_faces = origin.faces
        self.session = TransferSession(
            _counter[0], origin, self.fingerprint(obj, mesh, window), trigger=trigger, object=obj.name,
            uv_map=origin.uv_map, islands=origin.count, island_corners=int(len(origin.corners)),
            island_faces=int(len(origin.faces)), origin_digest=ctx.digest(origin.uv[origin.corners]),
            images=[{"image": e.key, "semantic": e.semantic, "status": e.status, "reason": e.reason}
                    for e in image_set.entries], blender=bpy.app.version_string)
        self.stable = origin.uv[origin.corners].copy()
        self.pre = None
        self.op_tail = ctx.registered_tail()
        self.transform_ends = 0
        self.shape, self.shaped = "translation", None
        self.last_check = time.perf_counter()
        uvs = mesh.corners.uv[origin.corners]
        for op in opening_ops:
            self.add_transform(op, uvs)
        self.stable = uvs.copy()
        self.update_shape()

    # -- views of the domain session

    id = property(lambda self: self.session.id)
    origin = property(lambda self: self.session.origin)
    state = property(lambda self: self.session.state)
    closed = property(lambda self: self.session.closed)
    result = property(lambda self: self.session.result)
    history = property(lambda self: self.session.history)
    events = property(lambda self: self.session.events)

    def last(self, event):
        return self.session.last(event)

    @property
    def targets(self):
        """The images Ctrl+Enter would write now, with the scene's current choices."""
        return [k for k in self.image_set().targets if k in self.frozen]

    def image_set(self):
        """The session's image set (materials as they were when it opened) with the current choices."""
        win = self.window()
        included, excluded = imageset.choices(win.scene if win is not None else bpy.context.scene)
        return resolve(self.facts, included, excluded)

    # -- context

    def window(self):
        for w in bpy.context.window_manager.windows:
            if w.as_pointer() == self.window_ptr:
                return w
        return None

    def fingerprint(self, obj, mesh, window):
        """The context fingerprint, the UVs outside the island, which no session move may change, the
        frozen images and what the island's materials make of their images."""
        fp = ctx.fingerprint(obj, mesh, window)
        same = len(mesh.corners.uv) == len(self.outside)
        fp["uvs_outside_island"] = ctx.digest(mesh.corners.uv[self.outside]) if same else "corner count changed"
        fp["images"] = self.image_state()
        fp["image_set"] = material_signature(obj, mesh, self.origin_faces, mesh.uv_map, window.scene)
        return fp

    def image_state(self):
        out = {}
        for key in self.frozen:
            img = bpy.data.images.get(key)
            out[key] = transfer.image_fingerprint(img) if img is not None else None
        return out

    def check_context(self):
        """(changed fingerprint keys, EditMeshUV or None). Image texels are compared at Ctrl+Enter."""
        obj = bpy.data.objects.get(self.object_name)
        if obj is None or obj.as_pointer() != self.object_ptr:
            return ["object"], None
        win = self.window()
        if win is None:
            return ["window"], None
        if obj.mode != "EDIT":
            return ["mode"], None
        try:
            mesh = mesh_uv.read(obj, self.origin.uv_map)
        except ValueError:
            return ["uv_map"], None
        fp = self.fingerprint(obj, mesh, win)
        return [k for k in self.session.fingerprint if fp.get(k) != self.session.fingerprint[k]], mesh

    # -- observation

    def poll(self, busy, transforms):
        if self.closed:
            return
        if self.window() is None:
            self.invalidate(["window"])
            return
        if self.state == "OPEN" and transforms:
            self.pre = self.stable.copy()
            self.session.begin_transform(transforms[0], mesh_reads="suspended")
            ctx.tag_redraw()
            return
        if busy:
            # Never touch the edit mesh while another modal operator runs.
            return
        now = time.perf_counter()
        if self.state == "OPEN" and not self.tool.input_seen and now - self.last_check < CHECK_INTERVAL:
            return
        self.last_check, self.tool.input_seen = now, False
        changed, mesh = self.check_context()
        if changed:
            self.invalidate(changed)
            return
        uvs = mesh.corners.uv[self.origin.corners]
        new = ctx.registered_since(self.op_tail) or []
        self.op_tail = ctx.registered_tail()
        transforms_done = [op for op in new if op.bl_idname.startswith("TRANSFORM_OT_")]
        if self.state == "TRANSFORMING":
            self.finish_transform(uvs, transforms_done)
        else:
            for op in transforms_done:      # a transform that started and ended between two ticks
                self.add_transform(op, uvs)
            if not np.array_equal(uvs, self.stable):
                self.session.note("UVsChangedOutsideTransform", registered=[op.bl_idname for op in new],
                                  max_uv_delta=float(np.abs(uvs - self.stable).max()))
            self.stable = uvs
            self.update_shape()
        ctx.tag_redraw()

    def add_transform(self, op, uvs):
        self.session.confirm_transform(op.bl_idname, ctx.op_props(op),
                                       max_uv_delta=float(np.abs(uvs - self.stable).max()) if len(uvs) else 0.0)

    def finish_transform(self, uvs, transforms_done):
        if transforms_done:
            for op in transforms_done:
                self.add_transform(op, uvs)
        else:
            self.session.cancel_transform(restored_exact=bool(np.array_equal(uvs, self.pre)),
                                          max_uv_diff=float(np.abs(uvs - self.pre).max()))
        self.transform_ends += 1
        self.stable = uvs
        self.update_shape()

    def update_shape(self):
        """What Ctrl+Enter will do with the islands as they are now: "translation", "affine" when any island
        was rotated or scaled, or the key of the refusal it will give (affine.REFUSALS), the first island's
        that has one. Each island has its own map."""
        if self.shaped is not None and np.array_equal(self.shaped, self.stable):
            return
        o, shape = self.origin, "translation"
        for k in range(o.count):
            idx = np.flatnonzero(o.corner_island == k)
            start, now = o.uv[o.corners[idx]], self.stable[idx]
            if measure_translation(start, now)[2] <= TRANSLATION_TOL:
                continue
            fit = fit_affine(start, now)
            if not fit.ok:
                shape = fit.verdict
                break
            shape = "affine"
        self.shape, self.shaped = shape, self.stable.copy()

    # -- commands

    def commit(self, area, margin):
        s = self.session
        s.begin_commit()
        changed, mesh = self.check_context()
        if changed:
            self.invalidate(changed)
            return
        obj = bpy.data.objects[self.object_name]
        if s.disallowed:
            self.reject("InvalidTransform", f"{', '.join(s.disallowed)} cannot be carried; only G, R and S",
                        disallowed=s.disallowed)
            return
        work = self.plan_own(obj, mesh, margin)
        if work is not None:
            self.publish_carry(obj, *work)

    def plan_own(self, obj, mesh, margin):
        """Each island carried in the images of its own materials. Returns what publish_carry takes, or None
        when Ctrl+Enter ended otherwise (deferred, refused or invalidated)."""
        current = self.image_set()
        verb = "padded" if self.padding else "carried"
        if current.blocking:
            reasons = "; ".join(f"{k} ({current.entry(k).reason})" for k in current.blocking)
            then = "then press Ctrl+Enter again" if self.padding else "or Cancel"
            self.defer(f"Not {verb}: {reasons}. Leave it out in the UV Carry panel (N > UV Carry), {then}",
                       blocking=list(current.blocking))
            return None
        keys = [k for k in current.targets if k in self.frozen]
        if not keys:
            hint = "include an image in the UV Carry panel (N > UV Carry)" if any(
                e.status == "optional" for e in current.entries) else \
                "connect a texture to the material through the edited UV map, or show it in the UV editor and " \
                "include it in the UV Carry panel"
            self.defer(f"No image to {'pad' if self.padding else 'carry into'}: {hint}")
            return None
        images = {k: bpy.data.images.get(k) for k in keys}
        count = self.origin.count
        island_targets = [self.reads[k] & set(keys) for k in range(count)] if self.reads is not None else None
        writers = {key: {k for k in range(count) if island_targets is None or key in island_targets[k]}
                   for key in keys}
        try:
            prepared = transfer.prepare(obj, self.origin, images, [self.frozen[k] for k in keys], margin,
                                        SETTINGS["memory_budget_mb"] * 2 ** 20, {k: current.entry(k).alpha for k in keys},
                                        self.slot_images, island_targets=island_targets)
        except ContextChanged as exc:       # an image edited under an island since the session opened
            self.invalidate(exc.changed)
            return None
        except TransferError as exc:
            self.reject(exc.category, str(exc), **exc.detail)
            return None

        def describe(report):
            entry = current.entry(report["image"])
            return {"semantic": entry.semantic, "alpha": entry.alpha}
        return prepared, images, {k: self.frozen[k] for k in keys}, describe

    def publish_carry(self, obj, prepared, images, held, describe):
        """Publish a prepared carry, keep its texels for undo and close the session."""
        s, plan = self.session, prepared.plan
        s.planned(kind=plan.kind, images=[t.target.key for t in plan.targets],
                  methods=[r["method"] for r in prepared.reports], bytes=plan.nbytes, snapped_px=plan.snapped_px,
                  grid=plan.grid, islands=len(plan.islands))
        written = [p.target for p in prepared.patches]       # a padding writes no image with nothing to fill
        frozen = [held[k] for k in written]
        self.carried = written
        result = transfer.publish(obj, self.origin, images, prepared, faults=FAULTS,
                                  revalidate=lambda i: self.revalidate(frozen, i), frozen=frozen)
        if not result.ok:
            self.fail(result)
            return
        for report in prepared.reports:
            s.note("TransferApplied", **describe(report), **report)
        summary = summarize(prepared.reports, plan)
        if written:
            try:
                carry_id = ledger.record(obj, self.origin.faces,
                                         [(p.target, p.region, p.before, p.after) for p in prepared.patches],
                                         SETTINGS["undo_budget_mb"] * 2 ** 20)
            except Exception as exc:     # recorded, not hidden: the carry stands, without undo
                carry_id = None
                s.note("UndoRecordError", error=repr(exc), traceback=traceback.format_exc())
            pushed = self.push_undo("UV Carry: padding" if plan.kind == "padding" else "UV Carry")
            s.note("UndoPush", result=pushed, carry=carry_id)
            what = "padding" if plan.kind == "padding" else "copy"
            undoable = "Ctrl+Z undoes it" if carry_id is not None and pushed == "ok" else f"Ctrl+Z cannot undo this {what}"
            persist.note_carried([images[k] for k in written])
            changed = "The image changed" if len(written) == 1 else "The images changed"
            message = (f"{summary}. {changed} in memory only (Save Carried Images in the UV Carry panel saves it); "
                       f"{undoable}.")
        else:
            if any(p.snap_to is not None for p in plan.islands):     # the islands went back: an undo step
                s.note("UndoPush", result=self.push_undo("UV Carry"), carry=None)
            message = f"{summary}. No image changed."
        s.committed(message, images=written, bytes=plan.nbytes)
        self.finish()

    def defer(self, message, **detail):
        """Ctrl+Enter carried nothing and the session stays open, for the user to adjust the image set. A
        padding has no session to keep open: it ends, having written nothing."""
        if self.padding:
            self.reject("UnsupportedMapping", message, **detail)
            return
        self.session.defer(message, **detail)
        self.tool.last = {"state": "OPEN", "severity": "WARNING", "message": message + "; the session stays open"}
        ctx.tag_redraw()

    @property
    def islands(self):
        return "Island" if self.origin.count == 1 else "Islands"

    def revalidate(self, frozen, i):
        """Before writing image i: the session's object, mode and mesh are still there, and the images not
        yet written are the same images (identity, size, format). Ctrl+Enter read the context whole moments
        before and nothing runs in between; transfer.publish compares the texels of image i as it writes it."""
        obj = bpy.data.objects.get(self.object_name)
        if obj is None or obj.as_pointer() != self.object_ptr:
            return ["object"]
        if obj.mode != "EDIT":
            return ["mode"]
        if obj.data.as_pointer() != self.session.fingerprint["mesh"]:
            return ["mesh"]
        return transfer.image_changes(frozen[i:])

    def fail(self, result):
        """Publishing failed: everything written was rolled back, or the rollback itself failed. A padding
        moved no island: there is nothing to put back."""
        if self.padding:
            restore, undo, back = "not needed", "skipped", ""
        else:
            restore = self.restore()
            undo = self.push_undo("UV Carry: carry failed") if restore == "exact" else "skipped"
            back = f"; {self.islands.lower()} restored to the session start: {restore}"
        what = "Padding" if self.padding else "Carry"
        if result.category == "RollbackFailure":
            message = (f"{what} failed and could not be rolled back ({result.rollback}). Some images may hold "
                       f"part of the {what.lower()}{back}.")
        elif result.category == "ContextChanged":
            message = f"{what} stopped ({describe(result.changed)}). Images restored{back}."
        else:
            message = f"{what} failed ({result.error}). Images restored{back}."
        self.session.commit_failed(result.category, message, result.rollback, restore, stage=result.stage,
                                   error=result.error, changed=result.changed, undo_push=undo)
        self.finish()

    def reject(self, category, message, **detail):
        message = message[:1].upper() + message[1:]
        if self.padding:
            self.session.reject(category, f"{message}. Nothing written.", "not needed", **detail)
            self.finish()
            return
        restore = self.restore()
        undo = self.push_undo("UV Carry: island restored") if restore == "exact" else "skipped"
        self.session.reject(category, f"{message}. {self.islands} restored to the session start: {restore}. "
                            "Image untouched.", restore, undo_push=undo, **detail)
        self.finish()

    def cancel(self, reason, push_undo):
        restore = self.restore()
        undo = (self.push_undo("UV Carry: session cancelled") if restore == "exact" else "skipped") \
            if push_undo else "by the operator"
        self.session.note("CancelRestore", undo_push=undo)
        self.session.cancel(reason, restore, f"Session cancelled. {self.islands} restored to the session start: "
                            f"{restore}.")
        self.finish()

    def invalidate(self, changed):
        restore = self.restore()
        undo = self.push_undo("UV Carry: session invalidated") if restore == "exact" else "skipped"
        self.session.note("RestoreAfterInvalidation", restore=restore, undo_push=undo)
        self.session.invalidate(changed, restore, f"Session invalidated ({describe(changed)}). "
                                f"{self.islands} restored to the session start: {restore}.")
        self.finish()

    def drop(self, reason):
        """End the session without touching the mesh or the images (file loading, undo, handler removed)."""
        if not self.closed:
            self.session.drop(reason, f"Session dropped ({reason}); the island was not restored.")
            self.finish()

    def restore(self):
        obj = bpy.data.objects.get(self.object_name)
        if obj is None or obj.as_pointer() != self.object_ptr:
            return "impossible: object missing"
        if obj.type != "MESH" or obj.data.as_pointer() != self.session.fingerprint["mesh"]:
            return "impossible: mesh replaced"
        o = self.origin
        try:
            topology, _ = ctx.corner_uvs(obj, o.uv_map)
            if topology != o.topology:
                return "impossible: topology changed"
            ctx.set_corner_uvs(obj, o.uv_map, o.corners, o.uv[o.corners])
            _, after = ctx.corner_uvs(obj, o.uv_map)
        except ValueError as exc:
            return f"impossible: {exc}"
        if np.array_equal(after[o.corners], o.uv[o.corners]):
            return "exact"
        return f"inexact (max diff {float(np.abs(after[o.corners] - o.uv[o.corners]).max()):.2e})"

    def push_undo(self, message):
        win = self.window()
        if win is None:
            return "no window"
        try:
            with bpy.context.temp_override(window=win):
                bpy.ops.ed.undo_push(message=message)
            return "ok"
        except Exception as exc:     # recorded, not hidden
            return f"error: {exc}"

    def finish(self):
        """After the session closed: log it and hand the tool back to READY."""
        result = self.result
        evidence.add(self.record())
        print(f"[UV Carry] {result['severity']}: {result['message']}")
        tool = self.tool
        if tool.session is self:
            tool.session = None
        tool.last, tool.input_seen = dict(result), True
        ctx.tag_redraw()

    def record(self):
        return {"session": self.id, "object": self.object_name, "uv_map": self.origin.uv_map,
                "trigger": self.trigger, "islands": self.origin.count,
                "island_corners": int(len(self.origin.corners)), "images": self.carried,
                "history": self.history, "result": self.result, "events": self.events}


# ---------------------------------------------------------------- tool

class Tool:
    def __init__(self, window):
        self.window_ptr = window.as_pointer()
        self.stop = False
        self.session = None
        self.snapshot = None
        self.read_tail = (-1, -1)
        self.status = ("unknown", None)    # selection status from the last read, shown while READY
        self.ready_set = None              # the image set a session would carry, from the last read
        self.input_seen = True
        self.press_area = None
        self.press_area_ptr = None
        self.tracking = None               # a transform running while READY, after a press over the UV editor
        self.last = None                   # last outcome shown to the user
        self.errors = 0

    def window(self):
        for w in bpy.context.window_manager.windows:
            if w.as_pointer() == self.window_ptr:
                return w
        return None

    # -- input

    def on_event(self, context, event):
        if event.value not in {"PRESS", "RELEASE", "DOUBLE_CLICK"} or event.type in NAVIGATION \
                or event.type in MODIFIER_KEYS:
            return {"PASS_THROUGH"}
        self.input_seen = True
        if event.value == "RELEASE" or event.is_repeat or ctx.locked():
            return {"PASS_THROUGH"}
        win = context.window
        area = ctx.area_under(win, event.mouse_x, event.mouse_y)
        self.press_area = area.ui_type if area else None
        if ctx.is_uv_editor(area):
            self.press_area_ptr = area.as_pointer()
        s = self.session
        if s is not None and event.type in TRACED_KEYS:
            s.session.note("InputSeen", key=event.type, ctrl=event.ctrl, shift=event.shift, alt=event.alt,
                           area=self.press_area, state=s.state)
        if event.type in COMMIT_KEYS and event.ctrl and not (event.shift or event.alt or event.oskey):
            return self.on_commit_chord(win, area, context.scene)
        if ctx.foreign_modal_ops(win):
            return {"PASS_THROUGH"}
        if event.type == "ESC" and s is not None and s.state == "OPEN":
            s.session.note("EscOutsideTransform", effect="none; Esc is reserved for the running transform")
        self.tick(at_press=True, uv_press=ctx.is_uv_editor(area))   # before Blender handles the press
        return {"PASS_THROUGH"}

    def on_commit_chord(self, win, area, scene):
        s = self.session
        if not ctx.is_uv_editor(area):
            if s is not None:
                s.session.note("CommitIgnored", reason="outside_uv_editor", area=area.ui_type if area else None)
            return {"PASS_THROUGH"}
        if ctx.foreign_modal_ops(win):
            return {"PASS_THROUGH"}
        ended = s.transform_ends if s is not None else None
        self.tick(at_press=True, uv_press=True)          # settle a transform that ended just before
        now = self.session
        if now is None:
            if s is None:
                self.pad(win, area, scene)
            return {"RUNNING_MODAL"}
        if now is not s or now.transform_ends != ended:
            now.session.note("CommitIgnored", reason="transform_ended_on_same_input")
            return {"RUNNING_MODAL"}
        if now.state != "OPEN":
            now.session.note("CommitIgnored", reason="session_not_open", state=now.state)
            return {"RUNNING_MODAL"}
        now.commit(area, scene.uv_carry_margin)
        return {"RUNNING_MODAL"}

    # -- state

    def tick(self, at_press=False, uv_press=False):
        win = self.window()
        if win is None:
            self.shutdown("window closed", restore=False)
            return
        busy = ctx.foreign_modal_ops(win)
        transforms = [n for n in busy if n.startswith("TRANSFORM_OT_")]
        if self.session is not None:
            self.session.poll(busy, transforms)
            if self.session is not None:
                return
        tracking = transforms[0] if transforms and self.snapshot is not None and self.press_area == "UV" else None
        if tracking != self.tracking:
            self.tracking = tracking
            ctx.tag_redraw()
        if busy:
            return
        self.settle_ready(win, refresh=uv_press or not at_press)

    def settle_ready(self, win, refresh):
        snap = self.snapshot
        if snap is None and self.status[0] == "multi_object":
            ops = ctx.registered_since(self.read_tail) or []
            moved = [op.bl_idname for op in ops if op.bl_idname.startswith("TRANSFORM_OT_")]
            if moved:
                n = self.status[1]["objects"]
                self.note("multi_object_move", f"Not carried: the UVs of {n} objects in Edit Mode moved without their "
                          "texture. Ctrl+Z puts them back; then leave Edit Mode, select only one object and press Tab "
                          "again", severity="WARNING", ops=moved, objects=n)
                refresh = True
        if snap is not None:
            ops = ctx.registered_since(snap.tail)
            if ops and any(op.bl_idname.startswith("TRANSFORM_OT_") for op in ops):
                self.try_open(win, snap, ops)
                if self.session is not None:
                    ctx.tag_redraw()
                    return
                refresh = True      # the read is stale now; never judge the same move twice
        if refresh and (self.input_seen or ctx.registered_tail() != self.read_tail):
            self.read_snapshot(win)

    def read_snapshot(self, win):
        self.read_tail, self.input_seen, self.snapshot = ctx.registered_tail(), False, None
        before = self.status
        obj = win.view_layer.objects.active
        editing = editing_meshes(win)
        if obj is None or obj.type != "MESH" or obj.mode != "EDIT":
            self.status = ("no_edit_mesh", None)
        elif len(editing) > 1:      # Blender would move every object's UVs; UV Carry would follow only obj's
            self.status = ("multi_object", {"objects": len(editing), "active": obj.name})
        elif win.scene.tool_settings.use_uv_select_sync:
            self.status = ("uv_sync", None)
        else:
            try:
                mesh = mesh_uv.read(obj)
            except ValueError as exc:
                self.status = ("no_uv_map", str(exc))
            else:
                self.snapshot = Snapshot(obj, mesh, self.read_tail)
                selection, origin = transfer.select_islands(mesh)
                self.status = (selection.kind, {"islands": origin.count, "faces": int(len(origin.faces))}
                               if origin is not None else None)
                if origin is not None:      # what a session would carry, shown before the move
                    self.ready_set = resolve_images(obj, mesh, origin, win.scene, self.press_uv_area(win))[0]
        if self.status[0] != "ok":
            self.ready_set = None
        if self.status != before:
            ctx.tag_redraw()

    def press_uv_area(self, win):
        for area in win.screen.areas:
            if area.as_pointer() == self.press_area_ptr and ctx.is_uv_editor(area):
                return area
        return next((a for a in win.screen.areas if ctx.is_uv_editor(a)), None)

    def try_open(self, win, snap, ops):
        """Open a session if the transforms registered since `snap` moved complete islands, and nothing else."""
        transforms = [op for op in ops if op.bl_idname.startswith("TRANSFORM_OT_")]
        names = [op.bl_idname for op in ops]
        obj = bpy.data.objects.get(snap.object_name)
        if obj is None or obj.as_pointer() != snap.object_ptr or obj.mode != "EDIT" \
                or obj.data.as_pointer() != snap.mesh_ptr:
            return                                   # the move happened in another context
        try:
            mesh = mesh_uv.read(obj, snap.mesh.uv_map)
        except ValueError:
            return
        before = snap.mesh
        if mesh.topology != before.topology or mesh.geometry != before.geometry:
            return                                   # vertices or topology changed: not a UV move
        moved = np.flatnonzero((mesh.corners.uv != before.corners.uv).any(axis=1))
        if not len(moved):
            return                                   # nothing moved
        other = [n for n in names if not n.startswith("TRANSFORM_OT_") and not any(q in n for q in QUIET_OPS)]
        if other:
            return self.note("other_edits", f"Move not tracked: {other[0]} ran between the last read and the "
                             "move. Undo the move (Ctrl+Z), then move the islands again", ops=names)
        if not np.array_equal(mesh.visible, before.visible):
            return self.note("shown_faces_changed", "Move not tracked: the faces shown in the UV editor changed")
        selection, origin = transfer.select_islands(mesh, uv=before.corners.uv)
        if origin is None:
            return self.note(f"selection_{selection.kind}", SELECTION_NOTES[selection.kind], detail=selection.detail)
        outside = np.ones(len(before.corners.uv), bool)
        outside[origin.corners] = False
        if outside[moved].any():
            return self.note("uvs_outside_island", "Move not tracked: it also moved UVs outside the selected islands "
                             "(proportional editing?)", corners=int(outside[moved].sum()))
        disallowed = sorted({op.bl_idname for op in transforms} - ALLOWED_TRANSFORMS)
        if disallowed:
            return self.note("disallowed_operator", f"Move not tracked: {disallowed[0]} is not carried; only G, R "
                             "and S are", ops=names)
        found = resolve_images(obj, mesh, origin, win.scene, self.press_uv_area(win))
        image_set, _, images = found
        keys = [e.key for e in image_set.entries    # every image a carry of this session may write, auto or included
                if e.status in ("auto", "optional") and images.get(e.key) is not None]
        reads, slots = island_reads(obj, mesh, origin, image_set)
        try:                                        # each image under the islands that read it
            regions = {k: transfer.snapshot_regions(images[k], origin, {i for i, r in enumerate(reads) if k in r})
                       for k in keys}
        except TransferError as exc:
            return self.note("images_not_frozen", f"Move not tracked: {exc}", category=exc.category)
        need, budget = sum(transfer.frozen_bytes(images[k], regions[k]) for k in keys), SETTINGS["memory_budget_mb"]
        if need > budget * 2 ** 20:
            return self.note("frozen_over_budget", f"Move not tracked: keeping the texels under {origin.count} "
                             f"islands takes {need / 2 ** 20:.0f} MB, over the memory budget of {budget} MB (add-on "
                             "preferences). Undo the move (Ctrl+Z) and move fewer islands at once",
                             needed=need, budget=budget * 2 ** 20)
        frozen = {k: transfer.freeze(images[k], origin, regions[k]) for k in keys}
        self.session = Runner(self, win, obj, origin, mesh, transforms, found, frozen, reads=reads, slot_images=slots)
        self.snapshot = None        # consumed: after the session, READY starts from a new read
        self.last = {"state": "OPEN", "severity": "INFO",
                     "message": "Session open. Ctrl+Enter over the UV editor carries the texture."}

    def pad(self, win, area, scene):
        """Ctrl+Enter over the UV editor with no session: pad the selected complete islands where they are.
        The padding runs as a session that commits as soon as it opens."""
        obj = win.view_layer.objects.active
        if obj is None or obj.type != "MESH" or obj.mode != "EDIT":
            return self.note("pad_no_edit_mesh", "Nothing to carry or pad: UV Carry needs a mesh in edit mode")
        editing = editing_meshes(win)
        if len(editing) > 1:
            return self.note("pad_multi_object", "Not padded: " + MULTI_OBJECT.format(n=len(editing), name=obj.name),
                             severity="WARNING", objects=len(editing))
        if scene.tool_settings.use_uv_select_sync:
            return self.note("pad_uv_sync", "Not padded: UV Sync Selection is on; turn it off to use UV Carry")
        try:
            mesh = mesh_uv.read(obj)
        except ValueError as exc:
            return self.note("pad_no_uv_map", f"Not padded: {exc}")
        selection, origin = transfer.select_islands(mesh)
        if origin is None:
            reason = "commit_without_session" if selection.kind == "empty" else f"pad_selection_{selection.kind}"
            return self.note(reason, PADDING_NOTES[selection.kind], detail=selection.detail)
        found = resolve_images(obj, mesh, origin, scene, area)
        image_set, _, images = found
        reads, slots = island_reads(obj, mesh, origin, image_set)
        frozen = {}
        for e in image_set.entries:     # a padding reads the islands' texels when it computes: nothing to freeze
            if e.status in ("auto", "optional") and images.get(e.key) is not None:
                try:
                    frozen[e.key] = transfer.watch(images[e.key])
                except TransferError as exc:
                    return self.note("images_not_padded", f"Not padded: {exc}", category=exc.category)
        self.session = Runner(self, win, obj, origin, mesh, [], found, frozen, trigger="padding", reads=reads,
                              slot_images=slots)
        self.snapshot = None
        self.session.commit(area, scene.uv_carry_margin)

    def note(self, reason, message, severity="INFO", **detail):
        evidence.add({"note": reason, "severity": severity, "message": message, "detail": detail})
        self.last = {"state": "READY", "severity": severity, "message": message}
        print(f"[UV Carry] {severity}: {message}")
        ctx.tag_redraw()

    def error(self, where, text):
        self.errors += 1
        evidence.add({"note": "tool_error", "severity": "ERROR", "where": where, "traceback": text})
        print(f"[UV Carry] ERROR in {where}:\n{text}")
        self.last = {"state": "FAILED", "severity": "ERROR", "message": f"Internal error in {where} (system console)"}
        if self.session is not None:
            try:
                self.session.session.note("ToolError", where=where)
                self.session.invalidate(["tool_error"])
            except Exception:
                self.session.drop("tool error")
        self.input_seen, self.read_tail = False, ctx.registered_tail()
        if self.errors >= 3:
            self.shutdown("repeated internal errors", restore=False)

    def shutdown(self, reason, restore=True):
        global TOOL
        s = self.session
        if s is not None and not s.closed:
            if restore and s.state == "OPEN":
                s.cancel(reason, push_undo=True)
            else:
                s.drop(reason)
        self.stop = True
        if TOOL is self:
            TOOL = None
        ctx.tag_redraw()


def tick_timer():
    t = TOOL
    if t is None or t.stop:
        return None
    if ctx.locked():                # a job is changing the mesh (UV > Pack Islands, for one): not read now
        return POLL_INTERVAL
    try:
        t.tick()
        t.errors = 0
    except Exception:
        t.error("tick", traceback.format_exc())
    return POLL_INTERVAL


def start(window):
    """Turn the tool on in `window`. Returns the Tool."""
    global TOOL
    TOOL = Tool(window)
    if not bpy.app.timers.is_registered(tick_timer):
        bpy.app.timers.register(tick_timer, first_interval=POLL_INTERVAL)
    ctx.tag_redraw()
    return TOOL


def after_undo_or_redo():
    """Bring carried texels in line with the undo step Blender just restored, tool on or off."""
    try:
        changed = ledger.sync()
    except Exception:
        evidence.add({"note": "undo_sync_error", "severity": "ERROR", "traceback": traceback.format_exc()})
        print(f"[UV Carry] ERROR while undoing carried texels:\n{traceback.format_exc()}")
        return
    if not changed:
        return
    undone = sorted({c for c, what in changed if what == "undone"})
    redone = sorted({c for c, what in changed if what == "redone"})
    parts = ([f"texture of carry {', '.join(map(str, undone))} restored"] if undone else []) + \
            ([f"carry {', '.join(map(str, redone))} applied again"] if redone else [])
    message = ("Undo: " if undone else "Redo: ") + "; ".join(parts)
    evidence.add({"note": "undo_sync", "severity": "INFO", "message": message, "detail": {"changed": changed}})
    print(f"[UV Carry] INFO: {message}")
    t = TOOL
    if t is not None:
        if t.session is not None and not t.session.closed:
            t.session.drop("undo or redo changed a carried texture")
        t.last, t.input_seen = {"state": "READY", "severity": "INFO", "message": message}, True
    ctx.tag_redraw()
