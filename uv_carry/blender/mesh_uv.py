"""Blender adapter: face corners, UV selection and loop triangles of a mesh in edit mode.

Call only when no other modal operator runs:
creating BMesh element wrappers reallocates element data under a running operator.
Corners are numbered in face order, which matches mesh loop indices while topology is unchanged.

Blender keeps no arrays of an edit mesh, and reading its elements one by one in Python costs about a
microsecond per corner. Reads write the edit mesh into a scratch mesh (BMesh.to_mesh, in C), read it with
foreach_get and empty it again. The scratch mesh is made once and kept: adding a mesh to bpy.data and
removing it on every read broke Blender's undo in edit mode (an undo then went past the edit steps). Its
name starts with a dot, so Blender lists it nowhere, and with no users it is never saved with the .blend.
release() removes it. Writes touch only the faces they change.
"""

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass

import bmesh
import bpy
import numpy as np

from ..domain.islands import FaceCorners

TEMP_MESH = ".uv_carry_read"        # the scratch mesh of the reads
UV_SELECT = ".uv_select_vert"       # Blender 5.1: UV vertex selection per corner, as written by to_mesh


@dataclass(frozen=True, eq=False)
class EditMeshUV:
    corners: FaceCorners
    selected: np.ndarray        # per corner: UV vertex selected
    visible: np.ndarray         # per face: shown in the UV editor (selected and not hidden)
    triangles: np.ndarray       # (T, 3) corner indices, Blender's own triangulation
    triangle_face: np.ndarray   # (T,) face of each triangle
    uv_map: str
    topology: str
    geometry: str = ""          # digest of the vertex positions
    active_uv_map: str = ""     # active UV map, which may differ from uv_map
    face_material: np.ndarray = None    # per face: material slot index

    @property
    def selection(self):
        """Digest of the selected corners of visible faces."""
        shown = self.selected & self.visible[self.corners.corner_face]
        return hashlib.sha1(np.flatnonzero(shown).tobytes()).hexdigest()[:16]


_scratch_name = [TEMP_MESH]         # the scratch mesh's name, should TEMP_MESH be taken


def _scratch():
    me = bpy.data.meshes.get(_scratch_name[0])
    if me is None or me.users or me.library is not None:     # gone (file load, undo, purge), or not ours
        me = bpy.data.meshes.new(TEMP_MESH)
        _scratch_name[0] = me.name
    return me


@contextmanager
def arrays(obj):
    """The edit mesh of obj written into the scratch mesh, to read with foreach_get; emptied on exit."""
    bm = bmesh.from_edit_mesh(obj.data)
    tmp = _scratch()
    try:
        bm.to_mesh(tmp)
        yield tmp
    finally:
        tmp.clear_geometry()


def release():
    """Remove the scratch mesh (unregister)."""
    me = bpy.data.meshes.get(_scratch_name[0])
    if me is not None and not me.users and me.library is None:
        bpy.data.meshes.remove(me)


def _get(seq, prop, dtype, count, width=1):
    out = np.zeros(count * width, dtype)
    if count:
        seq.foreach_get(prop, out)
    return out.reshape(-1, width) if width > 1 else out


def read(obj, uv_map=None):
    """Read the edit mesh of obj. Returns EditMeshUV, or raises ValueError with a reason."""
    if obj is None or obj.type != "MESH" or obj.mode != "EDIT":
        raise ValueError("needs a mesh object in edit mode")
    bm = bmesh.from_edit_mesh(obj.data)
    active = bm.loops.layers.uv.active
    layer = bm.loops.layers.uv.get(uv_map) if uv_map else active
    if layer is None:
        raise ValueError(f"UV map {uv_map!r} not found" if uv_map else "mesh has no UV map")
    with arrays(obj) as me:
        nf, nc, nv = len(me.polygons), len(me.loops), len(me.vertices)
        face_start = _get(me.polygons, "loop_start", np.int32, nf).astype(np.int64)
        face_len = _get(me.polygons, "loop_total", np.int32, nf).astype(np.int64)
        visible = _get(me.polygons, "select", bool, nf) & ~_get(me.polygons, "hide", bool, nf)
        material = _get(me.polygons, "material_index", np.int32, nf).astype(np.int64)
        corner_vert = _get(me.loops, "vertex_index", np.int32, nc).astype(np.int64)
        uv = _get(me.uv_layers[layer.name].uv, "vector", np.float32, nc, 2).astype(np.float64)
        selection = me.attributes.get(UV_SELECT)
        if selection is not None and selection.domain == "CORNER" and len(selection.data) == nc:
            selected = _get(selection.data, "value", bool, nc)
        else:                                   # not written: read the corners one by one
            selected = np.array([loop.uv_select_vert for f in bm.faces for loop in f.loops], bool)
        coords = _get(me.vertices, "co", np.float32, nv, 3)
        me.calc_loop_triangles()
        triangles = _get(me.loop_triangles, "loops", np.int32, len(me.loop_triangles), 3).astype(np.int64)
    triangles = triangles.reshape(-1, 3)
    face_of = np.repeat(np.arange(nf), face_len)
    corners = FaceCorners(face_start, face_len, corner_vert, uv.reshape(-1, 2))
    digest = hashlib.sha1(face_len.tobytes() + corner_vert.tobytes() + np.int64(nv).tobytes()).hexdigest()[:16]
    return EditMeshUV(corners, selected, visible, triangles,
                      face_of[triangles[:, 0]] if len(triangles) else np.zeros(0, np.int64), layer.name, digest,
                      hashlib.sha1(coords.tobytes()).hexdigest()[:16], active.name if active else "", material)


def face_ints(obj, name):
    """Values of the integer face attribute `name` of the edit mesh, or None when it has none."""
    with arrays(obj) as me:
        attr = me.attributes.get(name)
        if attr is None or attr.domain != "FACE" or attr.data_type != "INT":
            return None
        return _get(attr.data, "value", np.int32, len(attr.data)).astype(np.int64)


def write_uvs(obj, uv_map, corner_indices, values):
    """Set the UVs of the given corners (edit mode)."""
    bm = bmesh.from_edit_mesh(obj.data)
    layer = bm.loops.layers.uv.get(uv_map)
    if layer is None:
        raise ValueError(f"UV map {uv_map!r} not found")
    corner_indices = np.asarray(corner_indices, np.int64)
    values = np.asarray(values, np.float64)
    with arrays(obj) as me:
        face_start = _get(me.polygons, "loop_start", np.int32, len(me.polygons)).astype(np.int64)
    faces = np.searchsorted(face_start, corner_indices, side="right") - 1
    bm.faces.ensure_lookup_table()
    face, loops = -1, None
    for k in np.argsort(faces, kind="stable"):
        if faces[k] != face:
            face = faces[k]
            loops = list(bm.faces[int(face)].loops)
        loops[int(corner_indices[k] - face_start[face])][layer].uv = values[k]
    bmesh.update_edit_mesh(obj.data)
