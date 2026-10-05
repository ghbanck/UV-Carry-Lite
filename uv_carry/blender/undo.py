"""Undo and redo of carried texels together with the UVs.

Blender's undo restores the edit mesh but not pixels written through the Python API, and Python
cannot read the undo stack. Each carry keeps, per image, the texels of its write region before and
after the write, and stamps its id on the island's faces in a hidden face attribute (MARKER). Edit-mesh
and global undo steps restore that attribute with the rest of the mesh. After every undo and redo,
the stamps show which carries are in effect: carries no longer in effect get their old texels back,
newest first; carries in effect again get their new texels, oldest first.
"""

import bmesh
import bpy
import numpy as np

from . import image_io, mesh_uv

MARKER = ".uv_carry_stamp"          # internal attributes (leading dot) are hidden in Blender's UI
BUDGET_BYTES = 512 * 2 ** 20        # default cap on the texels kept; carries beyond it, oldest first, drop out
CARRIES = []                        # one entry per image write that undo or redo can still reach, oldest first
_last_id = [0]                      # ids only grow, so a stamp never names two carries


class Carry:
    def __init__(self, cid, obj, faces, image_name, region, before, after):
        self.id = cid
        self.object_name, self.mesh_name = obj.name, obj.data.name
        self.faces = np.asarray(faces, np.int64)
        self.image_name = image_name
        self.region, self.before, self.after = region, before, after
        self.applied = True

    @property
    def nbytes(self):
        return self.before.nbytes + self.after.nbytes


def stamps(obj):
    """Stamp of every face in face order, or None when the mesh carries no stamp."""
    if obj.mode == "EDIT":
        if bmesh.from_edit_mesh(obj.data).faces.layers.int.get(MARKER) is None:
            return None
        return mesh_uv.face_ints(obj, MARKER)
    attr = obj.data.attributes.get(MARKER)
    if attr is None or attr.domain != "FACE" or attr.data_type != "INT":
        return None
    out = np.zeros(len(attr.data), np.int32)
    attr.data.foreach_get("value", out)
    return out.astype(np.int64)


def record(obj, faces, writes, budget_bytes=BUDGET_BYTES):
    """Stamp a new carry on the island's faces (edit mode) and keep its texels. `writes` lists
    (image name, region, texels before, texels after) per image. Call after the image writes and
    before pushing the undo step that is to hold the stamp. Returns the carry id."""
    CARRIES[:] = [c for c in CARRIES if c.applied]      # the new step cuts off what redo could reach
    known = stamps(obj)
    cid = max([_last_id[0]] + [c.id for c in CARRIES] + ([int(known.max())] if known is not None and len(known) else [])) + 1
    _last_id[0] = cid
    bm = bmesh.from_edit_mesh(obj.data)
    layer = bm.faces.layers.int.get(MARKER) or bm.faces.layers.int.new(MARKER)
    bm.faces.ensure_lookup_table()
    for i in np.unique(np.asarray(faces, np.int64)):
        bm.faces[int(i)][layer] = cid
    bmesh.update_edit_mesh(obj.data)
    added = [Carry(cid, obj, faces, name, region, before, after) for name, region, before, after in writes]
    CARRIES.extend(added)
    while sum(c.nbytes for c in CARRIES) > budget_bytes and len(CARRIES) > len(added):
        CARRIES.pop(0)
    return cid


def sync():
    """After an undo or redo: write back the texels of every carry whose stamp changed state.
    Returns [(carry id, "undone" or "redone")], one entry per carry."""
    state = {}
    for c in CARRIES:
        obj = bpy.data.objects.get(c.object_name)
        if obj is None or obj.type != "MESH" or obj.data.name != c.mesh_name:
            state[c] = None
            continue
        s = stamps(obj)
        if s is None:
            state[c] = False
        elif len(s) <= int(c.faces.max()):
            state[c] = None                      # faces were removed: leave the texels as they are
        else:
            state[c] = bool(s[c.faces].max() >= c.id)
    changed = []
    for c in reversed(CARRIES):
        img = bpy.data.images.get(c.image_name)
        if state[c] is False and c.applied and img is not None:
            image_io.write_region(img, c.region, c.before)
            c.applied = False
            changed.append((c.id, "undone"))
    for c in CARRIES:
        img = bpy.data.images.get(c.image_name)
        if state[c] is True and not c.applied and img is not None:
            image_io.write_region(img, c.region, c.after)
            c.applied = True
            changed.append((c.id, "redone"))
    seen, out = set(), []
    for item in changed:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def clear():
    CARRIES.clear()
