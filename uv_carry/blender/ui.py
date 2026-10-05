"""What UV Carry shows: the header button, the sidebar panel and the overlay at the bottom of the UV
editor, all from the same status lines."""

import blf
import bpy

from ..domain.affine import REFUSALS
from . import controller, persist

CYAN, LIGHT, GREEN, ORANGE, RED = ((0.13, 0.85, 0.93, 1), (0.85, 0.88, 0.92, 1), (0.6, 0.95, 0.7, 1),
                                   (1, 0.72, 0.3, 1), (1, 0.45, 0.4, 1))
READY_TEXT = {
    "partial": "part of an island selected: select whole islands (L over each)",
    "empty": "select UV islands, then move them with G, R or S, or pad them with Ctrl+Enter",
    "no_edit_mesh": "needs a mesh in edit mode",
    "uv_sync": "UV Sync Selection is on: turn it off to use UV Carry",
    "no_uv_map": "the mesh has no UV map",
    "unknown": "select UV islands, then move them with G, R or S, or pad them with Ctrl+Enter",
}


def selected_text(detail):
    """"island of 32 faces selected, ..." or "3 islands (96 faces) selected, ..." for a READY selection."""
    n, faces = detail["islands"], detail["faces"]
    if n == 1:
        return f"island of {faces} faces selected, move it with G, R or S, or pad it with Ctrl+Enter"
    return f"{n} islands ({faces} faces) selected, move them with G, R or S, or pad them with Ctrl+Enter"


def image_text(image_set):
    """"  ·  images a.png, b.png" for the images a carry would write, with what holds it back."""
    if image_set is None:
        return ""
    names = list(image_set.targets)
    text = "  ·  no image to carry" if not names else \
        f"  ·  image {names[0]}" if len(names) == 1 else f"  ·  images {', '.join(names)}"
    if image_set.blocking:
        text += f" ({len(image_set.blocking)} blocked: see the UV Carry panel)"
    return text


def current_set(t):
    if t is None or t.stop:
        return None
    return t.session.image_set() if t.session is not None else t.ready_set


def status_lines(t, img):
    """[(text, color)] describing the tool state, shown in the overlay and the sidebar."""
    if t is None or t.stop:
        return []
    s = t.session
    if s is not None:
        moves = f"{len(s.history)} move{'s' if len(s.history) != 1 else ''}"
        count = s.origin.count
        what = "island" if count == 1 else "islands"
        if s.state == "TRANSFORMING":
            lines = [(f"UV Carry  ·  moving ({moves} so far)  ·  Esc cancels only this move", CYAN)]
        else:
            where = "" if count == 1 else f" on {count} islands"
            lines = [(f"UV Carry  ·  session open{where}, {moves}  ·  Ctrl+Enter carries the texture  ·  "
                      f"Cancel puts the {what} back{image_text(s.image_set())}", CYAN)]
        if s.shape == "affine":
            lines.append(("Rotated or scaled: Ctrl+Enter resamples the texture (bilinear)", LIGHT))
        elif s.shape != "translation":
            lines.append((f"{REFUSALS[s.shape]}: Ctrl+Enter will refuse and put the {what} back", ORANGE))
    elif t.tracking:
        lines = [("UV Carry  ·  moving: the session opens when this move is confirmed", CYAN)]
    else:
        kind, detail = t.status
        text = f"ready: {selected_text(detail)}" if kind == "ok" else READY_TEXT.get(kind, READY_TEXT["unknown"])
        shown = image_text(t.ready_set) if kind == "ok" else ""
        lines = [(f"UV Carry  ·  {text}{shown}", CYAN if kind == "ok" else LIGHT)]
    last = t.last
    if last and last.get("state") != "OPEN":
        color = {"INFO": GREEN if last["state"] == "COMMITTED" else LIGHT, "WARNING": RED, "ERROR": RED}
        lines.append((last["message"], color.get(last["severity"], LIGHT)))
    elif last and last.get("severity") == "WARNING":
        lines.append((last["message"], RED))
    return lines


def draw_overlay():
    t = controller.TOOL
    area, region = bpy.context.area, bpy.context.region
    if t is None or t.stop or area is None or region is None or area.ui_type != "UV":
        return
    scale = bpy.context.preferences.system.ui_scale
    font, size = 0, 12 * scale
    blf.size(font, size)
    x = 12 * scale
    for r in area.regions:        # stay clear of the Adjust Last Operation panel (bottom left)
        if r.type == "HUD" and r.width > 1 and r.height > 1:
            x = max(x, r.x - region.x + r.width + 12 * scale)
    width = max(region.width - x - 12 * scale, 100)
    rows = []
    for text, color in status_lines(t, getattr(bpy.context.space_data, "image", None)):
        line = ""
        for word in text.split(" "):
            trial = f"{line} {word}" if line else word
            if line and blf.dimensions(font, trial)[0] > width:
                rows.append((line, color))
                line = word
            else:
                line = trial
        rows.append((line, color))
    y = 10 * scale
    for text, color in reversed(rows):
        blf.color(font, *color)
        blf.position(font, x, y, 0)
        blf.draw(font, text)
        y += size * 1.6


def draw_header(self, context):
    if getattr(context.area, "ui_type", None) != "UV":
        return
    t = controller.TOOL
    on = t is not None and not t.stop
    row = self.layout.row(align=True)
    row.operator("uv_carry.toggle", text="UV Carry", icon="TEXTURE", depress=on)
    if on and t.session is not None:
        row.operator("uv_carry.cancel", text="", icon="LOOP_BACK")


def wrap(text, width):
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return lines + ([cur] if cur else [])


SEMANTIC_TEXT = {"color": "colour", "data": "data", "packed": "packed channels", "height": "height",
                 "normal_tangent": "tangent-space normal", "normal_object": "object-space normal",
                 "unknown": "not understood"}


def carried(entry, image_set):
    """Whether the checkbox of an entry is on: it is in the set (carried, or blocking the carry)."""
    return entry.key in image_set.targets or entry.key in image_set.blocking


def draw_image_set(layout, image_set):
    box = layout.box()
    box.label(text="Images", icon="IMAGE_DATA")
    if image_set is None:
        box.label(text="Select whole islands to see their images")
        return
    if not image_set.entries:
        box.label(text="The islands' materials reach no image")
        return
    for entry in image_set.entries:
        row = box.row(align=True)
        on = carried(entry, image_set)
        if entry.status == "unaffected":
            row.label(text="", icon="BLANK1")
        else:
            op = row.operator("uv_carry.image_choice", text="", emboss=False,
                              icon="CHECKBOX_HLT" if on else "CHECKBOX_DEHLT")
            op.image = entry.key
            op.choice = "exclude" if on else ("include" if entry.status == "optional" else "default")
        icon = {"blocked": "ERROR", "unaffected": "INFO"}.get(entry.status, "NONE")
        row.label(text=f"{entry.key}: {SEMANTIC_TEXT.get(entry.semantic, entry.semantic)}", icon=icon)
        notes = list(entry.roles[:3])
        if entry.reason:
            notes.append(entry.reason)
        notes += list(entry.diagnostics)
        for note in notes:
            for line in wrap(note, 40):
                box.label(text="    " + line)


class UVCARRY_PT_panel(bpy.types.Panel):
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = "UV Carry"
    bl_label = "UV Carry"

    def draw(self, context):
        col = self.layout.column()
        t = controller.TOOL
        on = t is not None and not t.stop
        col.operator("uv_carry.toggle", text="On" if on else "Off", icon="TEXTURE", depress=on)
        col.prop(context.scene, "uv_carry_margin")
        col.operator("uv_carry.cancel", icon="LOOP_BACK")
        img = getattr(context.space_data, "image", None)
        lines = status_lines(t, img)
        if lines:
            box = col.box()
            for text, _ in lines:
                for line in wrap(text.replace("  ·  ", ". "), 34):
                    box.label(text=line)
        if on:
            draw_image_set(col, current_set(t))
        draw_saving(col)


KIND_TEXT = {"file": "unsaved", "packed": "unsaved, packed", "generated": "no file: Image > Save As"}


def draw_saving(layout):
    """The images carries changed that are unsaved, and the button that saves them."""
    entries = persist.pending()
    if not entries:
        return
    box = layout.box()
    box.label(text="Unsaved carried images", icon="FILE_TICK")
    for img, kind in entries:
        box.label(text=f"{img.name}: {KIND_TEXT[kind]}", icon="ERROR" if kind == "generated" else "IMAGE_DATA")
    box.operator("uv_carry.save_images", icon="FILE_TICK")
