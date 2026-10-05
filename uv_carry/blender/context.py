"""Blender adapter: what the tool observes around a session. Running modal operators, registered
operators, the area under the mouse, the context fingerprint and the island's UVs in edit or object
mode."""

import hashlib
import json

import bpy
import numpy as np

from . import mesh_uv

OWN_PREFIX = "UV_CARRY_OT"          # the tool's own window handler, as Window.modal_operators names it


def foreign_modal_ops(window):
    """bl_idnames of running modal operators other than the tool's own handler."""
    return [op.bl_idname for op in window.modal_operators if not op.bl_idname.upper().startswith(OWN_PREFIX)]


def locked():
    """Whether a job holds the interface: Pack Islands, invoked from the UI, packs in a job that writes the UVs
    from another thread and registers before it ends. Nothing reads the mesh until the job ends."""
    return bpy.context.window_manager.is_interface_locked


def registered_tail():
    ops = bpy.context.window_manager.operators
    return (len(ops), ops[-1].as_pointer() if len(ops) else 0)


def registered_since(tail):
    """Operators registered after `tail`, oldest first; None when `tail` is no longer listed."""
    newer = []
    for op in reversed(list(bpy.context.window_manager.operators)):
        if op.as_pointer() == tail[1]:
            return newer[::-1]
        newer.append(op)
    return newer[::-1] if tail[1] == 0 else None


def op_props(op):
    out = {}
    for p in op.properties.bl_rna.properties:
        if p.identifier == "rna_type" or p.type in {"POINTER", "COLLECTION"}:
            continue
        v = getattr(op.properties, p.identifier)
        if not isinstance(v, str):
            try:
                v = list(v)
            except TypeError:
                pass
        out[p.identifier] = v
    return json.loads(json.dumps(out, default=str))


def area_under(window, x, y):
    for area in window.screen.areas:
        if area.x <= x < area.x + area.width and area.y <= y < area.y + area.height:
            return area
    return None


def is_uv_editor(area):
    return area is not None and area.type == "IMAGE_EDITOR" and area.ui_type == "UV"


def digest(a):
    return hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest()[:12]


def tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "IMAGE_EDITOR":
                area.tag_redraw()


def fingerprint(obj, mesh, window):
    return {"object": obj.as_pointer(), "active_object": window.view_layer.objects.active == obj,
            "mode": obj.mode, "mesh": obj.data.as_pointer(), "uv_map_active": mesh.active_uv_map,
            "topology": mesh.topology, "geometry": mesh.geometry, "selection": mesh.selection,
            "materials": material_state(obj, mesh)}


def material_state(obj, mesh):
    """The object's material slots and the slot of every face, as a digest."""
    slots = "|".join(f"{s.link}:{s.material.as_pointer() if s.material else 0}" for s in obj.material_slots)
    faces = mesh.face_material if mesh.face_material is not None else np.zeros(0, np.int64)
    return slots + "#" + digest(faces)


def corner_uvs(obj, uv_map):
    """(topology digest, every corner's UV) in edit or object mode; digest as in mesh_uv.read."""
    if obj.mode == "EDIT":
        mesh = mesh_uv.read(obj, uv_map)
        return mesh.topology, mesh.corners.uv
    me = obj.data
    layer = me.uv_layers.get(uv_map)
    if layer is None:
        raise ValueError(f"UV map {uv_map!r} not found")
    uv = np.zeros(len(me.loops) * 2, np.float32)
    layer.data.foreach_get("uv", uv)
    face_len = np.zeros(len(me.polygons), np.int32)
    me.polygons.foreach_get("loop_total", face_len)
    corner_vert = np.zeros(len(me.loops), np.int32)
    me.loops.foreach_get("vertex_index", corner_vert)
    topology = hashlib.sha1(face_len.astype(np.int64).tobytes() + corner_vert.astype(np.int64).tobytes()
                            + np.int64(len(me.vertices)).tobytes()).hexdigest()[:16]
    return topology, uv.reshape(-1, 2).astype(np.float64)


def set_corner_uvs(obj, uv_map, corners, values):
    """Set the UVs of some corners in edit or object mode."""
    if obj.mode == "EDIT":
        mesh_uv.write_uvs(obj, uv_map, corners, values)
        return
    me = obj.data
    layer = me.uv_layers.get(uv_map)
    if layer is None:
        raise ValueError(f"UV map {uv_map!r} not found")
    uv = np.zeros(len(me.loops) * 2, np.float32)
    layer.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    uv[np.asarray(corners)] = values
    layer.data.foreach_set("uv", uv.ravel())
    me.update()
