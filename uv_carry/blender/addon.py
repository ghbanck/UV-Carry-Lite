"""Registration of UV Carry in Blender: preferences, operators, the header button, the panel, the overlay
and the handlers for file loading and undo."""

import traceback

import bpy
from bpy.app.handlers import persistent

from .. import __package__ as BASE_PACKAGE
from . import context as ctx
from . import controller, evidence, mesh_uv, persist, ui
from . import undo as ledger


def apply_preferences(prefs=None):
    """Copy the add-on preferences into the running tool's settings. None when UV Carry is not
    installed as an add-on (test runs): the defaults stay."""
    if prefs is None:
        addon = bpy.context.preferences.addons.get(BASE_PACKAGE)
        prefs = addon.preferences if addon is not None else None
    if prefs is None:
        return None
    controller.SETTINGS.update(memory_budget_mb=prefs.memory_budget_mb, undo_budget_mb=prefs.undo_budget_mb)
    evidence.TRACE_DIR = bpy.path.abspath(prefs.trace_folder) if prefs.trace_folder else None
    return None


def _on_preference(self, _context):
    apply_preferences(self)


class UVCARRY_Preferences(bpy.types.AddonPreferences):
    bl_idname = BASE_PACKAGE

    memory_budget_mb: bpy.props.IntProperty(
        name="Memory Budget (MB)", default=4096, min=16, soft_max=16384, update=_on_preference,
        description="Largest amount of memory one carry may take, as estimated before it writes anything: the "
                    "texel copies it keeps (frozen sources, rollback snapshots, patches) and its working arrays. "
                    "A carry that needs more is refused before any write")
    undo_budget_mb: bpy.props.IntProperty(
        name="Undo Memory (MB)", default=512, min=0, soft_max=16384, update=_on_preference,
        description="Texels kept so that Ctrl+Z and Ctrl+Shift+Z can restore carried textures. Beyond it, the "
                    "oldest carries can no longer be undone")
    trace_folder: bpy.props.StringProperty(
        name="Trace Folder", subtype="DIR_PATH", default="", update=_on_preference,
        description="When set, every session is written to trace.json in this folder, for test runs")

    def draw(self, _context):
        col = self.layout.column()
        col.prop(self, "memory_budget_mb")
        col.prop(self, "undo_budget_mb")
        col.prop(self, "trace_folder")


class UVCARRY_ImageChoice(bpy.types.PropertyGroup):
    """The user's choice for one image: include an optional one, or leave out one the carry would write."""

    image: bpy.props.PointerProperty(type=bpy.types.Image)
    include: bpy.props.BoolProperty(default=False)


class UVCARRY_OT_image_choice(bpy.types.Operator):
    """Include an image in the carry, or leave it out"""
    bl_idname = "uv_carry.image_choice"
    bl_label = "Include or Leave Out"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    image: bpy.props.StringProperty()
    choice: bpy.props.EnumProperty(items=(("include", "Include", "Carry this image"),
                                          ("exclude", "Leave Out", "Do not carry this image"),
                                          ("default", "Default", "Back to what UV Carry decides")))

    def execute(self, context):
        img = bpy.data.images.get(self.image)
        if img is None:
            return {"CANCELLED"}
        items = context.scene.uv_carry_images
        index = next((i for i, item in enumerate(items) if item.image == img), None)
        if self.choice == "default":
            if index is not None:
                items.remove(index)
        else:
            item = items[index] if index is not None else items.add()
            item.image, item.include = img, self.choice == "include"
        for area in context.screen.areas if context.screen else ():
            area.tag_redraw()
        return {"FINISHED"}


class UVCARRY_OT_toggle(bpy.types.Operator):
    """Turn UV Carry on or off. While on, moving complete UV islands opens a session, Ctrl+Enter over the UV editor carries their texture, and Ctrl+Enter with no move pads them"""
    bl_idname = "uv_carry.toggle"
    bl_label = "UV Carry"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        t = controller.TOOL
        if t is not None and not t.stop:
            t.shutdown("tool turned off", restore=True)
            self.report({"INFO"}, "UV Carry off")
            return {"FINISHED"}
        if context.window is None:
            return {"CANCELLED"}
        self.tool = controller.start(context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        t = self.tool
        if t is not controller.TOOL or t.stop:
            return {"FINISHED", "PASS_THROUGH"}
        try:
            return t.on_event(context, event)
        except Exception:
            t.error("event", traceback.format_exc())
            return {"PASS_THROUGH"}

    def cancel(self, _context):
        if self.tool is controller.TOOL:
            controller.TOOL.shutdown("modal handler removed", restore=False)


class UVCARRY_OT_save_images(bpy.types.Operator):
    """Save the images carries changed: files over themselves after a backup, packed images packed again"""
    bl_idname = "uv_carry.save_images"
    bl_label = "Save Carried Images"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, _context):
        return any(kind != "generated" for _, kind in persist.pending())

    def execute(self, context):
        results = persist.save(persist.pending(), controller.FAULTS)
        severity, message = persist.summary(results)
        evidence.add({"note": "save_images", "severity": severity, "message": message, "detail": {"results": results}})
        print(f"[UV Carry] {severity}: {message}")
        t = controller.TOOL
        if t is not None:
            t.last = {"state": "READY" if t.session is None else "OPEN", "severity": severity, "message": message}
        ctx.tag_redraw()
        self.report({severity}, message)
        return {"FINISHED"}


class UVCARRY_OT_cancel(bpy.types.Operator):
    """Cancel the UV Carry session and put the island back where the session started"""
    bl_idname = "uv_carry.cancel"
    bl_label = "Cancel Session"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        t = controller.TOOL
        return (t is not None and t.session is not None and t.session.state == "OPEN"
                and context.window is not None and not ctx.foreign_modal_ops(context.window))

    def execute(self, context):
        t = controller.TOOL
        t.tick()
        s = t.session
        if s is None or s.state != "OPEN":
            self.report({"WARNING"}, "No open session")
            return {"CANCELLED"}
        s.cancel("cancel button", push_undo=False)    # the operator's UNDO flag records the step
        self.report({"INFO"}, s.result["message"])
        return {"FINISHED"}


@persistent
def on_load_pre(*_args):
    if controller.TOOL is not None:
        controller.TOOL.shutdown("file loading", restore=False)
    ledger.clear()
    persist.forget_all()


@persistent
def on_undo_or_redo(*_args):
    controller.after_undo_or_redo()


_draw_handles = []
CLASSES = (UVCARRY_Preferences, UVCARRY_ImageChoice, UVCARRY_OT_image_choice, UVCARRY_OT_toggle, UVCARRY_OT_cancel,
           UVCARRY_OT_save_images, ui.UVCARRY_PT_panel)
HANDLERS = ((bpy.app.handlers.load_pre, on_load_pre), (bpy.app.handlers.undo_post, on_undo_or_redo),
            (bpy.app.handlers.redo_post, on_undo_or_redo))


def register():
    bpy.types.Scene.uv_carry_margin = bpy.props.IntProperty(
        name="Margin (px)", default=2, min=0, max=16,
        description="Texels added around the islands from their own edge texels. A carry writes them over other UVs "
                    "too (D1); Ctrl+Enter with no move fills only texels no UV uses")
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.uv_carry_images = bpy.props.CollectionProperty(type=UVCARRY_ImageChoice)
    bpy.types.IMAGE_HT_header.append(ui.draw_header)
    _draw_handles.append(bpy.types.SpaceImageEditor.draw_handler_add(ui.draw_overlay, (), "WINDOW", "POST_PIXEL"))
    for handlers, fn in HANDLERS:
        if fn not in handlers:
            handlers.append(fn)
    bpy.app.timers.register(apply_preferences, first_interval=0.1)


def unregister():
    if controller.TOOL is not None:
        controller.TOOL.shutdown("unregistered", restore=True)
    if bpy.app.timers.is_registered(controller.tick_timer):
        bpy.app.timers.unregister(controller.tick_timer)
    mesh_uv.release()
    for handlers, fn in HANDLERS:
        if fn in handlers:
            handlers.remove(fn)
    while _draw_handles:
        bpy.types.SpaceImageEditor.draw_handler_remove(_draw_handles.pop(), "WINDOW")
    bpy.types.IMAGE_HT_header.remove(ui.draw_header)
    del bpy.types.Scene.uv_carry_images
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.uv_carry_margin
