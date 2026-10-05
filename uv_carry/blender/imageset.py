"""Blender adapter: the images an island's materials reach, and how.

Starting from each active Material Output of the materials on the island's faces, the node graph is
walked upstream. An Image Texture node feeding a Principled BSDF input, directly, through Separate
Color (packed channels), through a Normal Map node, or through Bump or Displacement (height), gets a
usage UV Carry understands. Any other node on the way (mixes, maths, groups, other shaders) gives a
usage it does not understand. Each image node's coordinates are then followed: the edited UV map
(directly, by a UV Map node or Texture Coordinate > UV, through an identity Mapping) makes the image
move with the island. Another UV map or other coordinates leave it unaffected. A Mapping node that
moves, turns or scales the edited UV map's coordinates, or another projection, makes it impossible to
carry. The facts go to uv_carry.domain.semantics, which decides the image set.
"""

import bpy

from ..domain.semantics import ImageFacts, Usage, resolve

PASS_THROUGH = {"REROUTE"}
EDITOR = "the UV editor only"


def _linked(socket):
    """(node, output socket) feeding an input socket, through reroutes; None when unlinked or muted."""
    seen = 0
    while socket.is_linked and seen < 64:
        link = socket.links[0]
        if link.is_muted or not link.is_valid:
            return None
        node, out = link.from_node, link.from_socket
        if node.type in PASS_THROUGH:
            socket, seen = node.inputs[0], seen + 1
            continue
        return node, out
    return None


def _images_upstream(node, depth=0, seen=None):
    """Every (Image Texture node, its output socket) feeding `node`, at any depth."""
    seen = set() if seen is None else seen
    if depth > 64 or node.as_pointer() in seen:
        return []
    seen.add(node.as_pointer())
    out = []
    for socket in node.inputs:
        found = _linked(socket)
        if found is None:
            continue
        upstream, out_socket = found
        if upstream.type == "TEX_IMAGE":
            out.append((upstream, out_socket))
        else:
            out.extend(_images_upstream(upstream, depth + 1, seen))
    return out


def _label(node):
    return node.label or node.bl_label or node.type.title()


def _input_usages(material, principled, socket, edited_map, active_map):
    """Usages of the images that feed one Principled input."""
    found = _linked(socket)
    if found is None:
        return []
    node, out = found
    name = socket.name
    if node.type == "TEX_IMAGE":
        return [(node, Usage(material.name, "principled", name, out.name))]
    if node.type == "SEPARATE_COLOR" and getattr(node, "mode", "RGB") == "RGB":
        src = _linked(node.inputs["Color"])
        if src is not None and src[0].type == "TEX_IMAGE":
            return [(src[0], Usage(material.name, "packed", name, src[1].name, channel=out.name))]
    if node.type == "NORMAL_MAP" and name in ("Normal", "Coat Normal"):
        src = _linked(node.inputs["Color"])
        if src is not None and src[0].type == "TEX_IMAGE":
            basis = node.uv_map or active_map
            if node.space == "TANGENT" and basis != edited_map:
                return [(src[0], Usage(material.name, "unknown", name, src[1].name,
                                       detail=f"a Normal Map on UV map {basis}"))]
            return [(src[0], Usage(material.name, "normal_map", name, src[1].name, space=node.space))]
    if node.type == "BUMP" and name in ("Normal", "Coat Normal"):
        usages = []
        src = _linked(node.inputs["Height"])
        if src is not None and src[0].type == "TEX_IMAGE":
            usages.append((src[0], Usage(material.name, "bump", name, src[1].name)))
        elif src is not None:
            usages += [(img, Usage(material.name, "unknown", name, o.name, detail=_label(src[0])))
                       for img, o in _images_upstream(src[0])]
        normal = node.inputs.get("Normal")
        if normal is not None and normal.is_linked:
            usages += _input_usages(material, principled, _Proxy(normal, name), edited_map, active_map)
        return usages
    return [(img, Usage(material.name, "unknown", name, o.name, detail=_label(node)))
            for img, o in [(node, out)] + _images_upstream(node) if img.type == "TEX_IMAGE"]


class _Proxy:
    """An input socket seen under another name (Bump's Normal input standing for the Principled input)."""

    def __init__(self, socket, name):
        self._socket, self.name = socket, name

    def __getattr__(self, attr):
        return getattr(self._socket, attr)


def material_usages(material, edited_map, active_map):
    """[(Image Texture node, Usage)] for the active outputs of one material."""
    tree = material.node_tree if material is not None and material.use_nodes else None
    if tree is None:
        return []
    out = []
    for node in tree.nodes:
        if node.type != "OUTPUT_MATERIAL" or not node.is_active_output:
            continue
        surface = _linked(node.inputs["Surface"])
        if surface is not None:
            shader, _ = surface
            if shader.type == "BSDF_PRINCIPLED":
                for socket in shader.inputs:
                    out += _input_usages(material, shader, socket, edited_map, active_map)
            else:
                out += [(img, Usage(material.name, "unknown", "Surface", o.name, detail=_label(shader)))
                        for img, o in _images_upstream(shader)]
        disp = _linked(node.inputs["Displacement"])
        if disp is not None:
            dnode, _ = disp
            src = _linked(dnode.inputs["Height"]) if dnode.type == "DISPLACEMENT" else None
            if src is not None and src[0].type == "TEX_IMAGE":
                out.append((src[0], Usage(material.name, "displacement", "Displacement", src[1].name)))
            else:
                out += [(img, Usage(material.name, "unknown", "Displacement", o.name, detail=_label(dnode)))
                        for img, o in _images_upstream(dnode)]
    return out


def _identity_mapping(node):
    if node.vector_type not in ("POINT", "TEXTURE"):
        return False
    for name, value in (("Location", 0.0), ("Rotation", 0.0), ("Scale", 1.0)):
        socket = node.inputs.get(name)
        if socket is None:
            continue
        if socket.is_linked or any(abs(v - value) > 1e-9 for v in socket.default_value):
            return False
    return True


def coordinates(node, active_map):
    """How an Image Texture node is mapped: ("uv", map name), ("blocked", reason) when the edited UV map
    reaches it changed, or ("other", reason)."""
    if node.projection != "FLAT":
        return "blocked", f"{node.projection.lower()} projection"
    socket, changed = node.inputs["Vector"], None
    for _ in range(64):
        found = _linked(socket)
        if found is None:
            return ("uv", active_map) if changed is None else ("blocked", changed)
        src, out = found
        if src.type == "UVMAP":
            name = src.uv_map or active_map
            return ("uv", name) if changed is None else ("blocked", changed)
        if src.type == "TEX_COORD":
            if out.name == "UV":
                return ("uv", active_map) if changed is None else ("blocked", changed)
            return "other", f"{out.name} coordinates"
        if src.type == "MAPPING":
            if not _identity_mapping(src):
                changed = changed or "a Mapping node that moves, turns or scales its coordinates"
            socket = src.inputs["Vector"]
            continue
        return "other", f"coordinates from {_label(src)}"
    return "other", "coordinates UV Carry cannot follow"


def _shared(img, materials, obj):
    users = []
    for mat in bpy.data.materials:
        if mat in materials or not mat.use_nodes or mat.node_tree is None:
            continue
        if any(n.type == "TEX_IMAGE" and n.image == img for n in mat.node_tree.nodes):
            users.append(f"material {mat.name}")
    for other in bpy.data.objects:
        if other == obj or other.type != "MESH":
            continue
        if any(s.material in materials for s in other.material_slots):
            users.append(f"object {other.name}")
    return tuple(users[:4]) + (("...",) if len(users) > 4 else ())


def island_materials(obj, mesh, faces):
    slots = sorted({int(i) for i in mesh.face_material[faces]}) if mesh.face_material is not None else [0]
    return [obj.material_slots[i].material for i in slots
            if i < len(obj.material_slots) and obj.material_slots[i].material is not None]


def collect(obj, mesh, faces, edited_map, editor_image=None):
    """(ImageFacts per image, {key: image}) for the images the island's faces reach."""
    materials = island_materials(obj, mesh, faces)
    active = next((layer.name for layer in obj.data.uv_layers if layer.active_render), edited_map)
    per_image, images = {}, {}
    for mat in materials:
        for node, usage in material_usages(mat, edited_map, active):
            img = node.image
            if img is None:
                continue
            record = per_image.setdefault(img.name, {"usages": [], "unmapped": [], "blocked": []})
            images[img.name] = img
            how, value = coordinates(node, active)
            if how == "uv" and value == edited_map:
                record["usages"].append(usage)
            elif how == "uv":
                record["unmapped"].append(f"UV map {value}")
            elif how == "blocked":
                record["usages"].append(usage)
                record["blocked"].append(value)
            else:
                record["unmapped"].append(value)
    if editor_image is not None and editor_image.name not in images and editor_image.source in ("FILE", "GENERATED") \
            and editor_image.size[0] and editor_image.size[1]:
        images[editor_image.name] = editor_image
        per_image[editor_image.name] = {"usages": [Usage("", "unknown", "UV editor", "Color", detail=EDITOR)],
                                        "unmapped": [], "blocked": []}
    facts = []
    for key, rec in per_image.items():
        img = images[key]
        width, height = int(img.size[0]), int(img.size[1])
        facts.append(ImageFacts(
            key, width, height, int(img.channels), img.source, bool(img.has_data or (width and height)),
            img.colorspace_settings.name, img.alpha_mode, tuple(dict.fromkeys(rec["usages"])),
            tuple(dict.fromkeys(rec["unmapped"])), tuple(dict.fromkeys(rec["blocked"])),
            _shared(img, materials, obj)))
    return facts, images


def slot_images(obj, mesh, edited_map):
    """{material slot index: the image keys its material reaches on the edited UV map}, for every slot the
    mesh's faces use: an island writes the images of its own materials."""
    active = next((layer.name for layer in obj.data.uv_layers if layer.active_render), edited_map)
    slots = sorted({int(i) for i in mesh.face_material}) if mesh.face_material is not None else [0]
    out = {}
    for i in slots:
        mat = obj.material_slots[i].material if i < len(obj.material_slots) else None
        keys = set()
        for node, _ in material_usages(mat, edited_map, active) if mat is not None else ():
            how, value = coordinates(node, active)
            if node.image is not None and how in ("uv", "blocked") and (how == "blocked" or value == edited_map):
                keys.add(node.image.name)
        out[i] = keys
    return out


def choices(scene):
    """(included, excluded) image names from the scene's choices."""
    included, excluded = set(), set()
    for item in getattr(scene, "uv_carry_images", ()):
        if item.image is not None:
            (included if item.include else excluded).add(item.image.name)
    return included, excluded


def image_set(obj, mesh, faces, edited_map, scene, editor_image=None):
    """(ImageSet, facts, {key: image}) for the island, with the scene's choices applied."""
    facts, images = collect(obj, mesh, faces, edited_map, editor_image)
    included, excluded = choices(scene)
    return resolve(facts, included, excluded), facts, images
