"""UV Carry - Blender add-on entry point. Blender baseline: 5.1.

Move, rotate or scale complete UV islands; their texture follows them. The Blender modules load only on
register, so `uv_carry.domain` stays importable without Blender (tests/domain).
"""


def register():
    from .blender import addon
    addon.register()


def unregister():
    from .blender import addon
    addon.unregister()
