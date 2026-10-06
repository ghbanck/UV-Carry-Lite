# UV Carry Lite 0.4.1

UV Carry Lite is a Blender 5.1 add-on. Move, rotate or scale complete UV islands, press Ctrl+Enter, and the texture under them follows them, in every image of their materials.

It needs Blender 5.1 and was tested with Blender 5.1.1 on Windows 11.

## Install

1. In Blender 5.1, open Edit > Preferences > Get Extensions.
2. Open the menu at the top right, choose Install from Disk, and pick this zip. Dropping the zip on the Blender window does the same.
3. UV Carry appears in the UV editor's header. Its preferences (memory budget, undo memory, trace folder) are under Edit > Preferences > Add-ons > UV Carry Lite.

To remove it: Edit > Preferences > Add-ons > UV Carry Lite, open its menu and choose Uninstall.

## Quick start

1. In the 3D View, select the mesh and press Tab for Edit Mode: the UV editor shows its UVs. Pick the texture in the UV editor's image menu to see it under them.
2. In the UV editor, press UV Carry in the header, or N > UV Carry > On.
3. Select complete UV islands: L over each, or A for all of them.
4. Move, rotate or scale them with G, R and S.
5. Press Ctrl+Enter over the UV editor: the texture follows the islands.
6. Save Carried Images, in the UV Carry panel, writes the changed images to disk.

Ctrl+Enter with complete islands selected and no move pads them instead: it fills the texels around them that no UV uses. USAGE.md, in this package, is the full guide.

## In this package

| File | What it is |
| --- | --- |
| `README.md` | This file. |
| `USAGE.md` | How to use UV Carry Lite: carrying, several islands, padding, saving, undo and its limits. |
| `LICENSE` | The GNU General Public License, version 3. |
| `blender_manifest.toml`, `__init__.py`, `blender/`, `domain/` | The add-on. |

## Good to know

- Only complete UV islands are carried. Mirrored islands, UDIM tiles and islands outside the 0 to 1 tile are refused before anything is written.
- Each island carries the images of its own materials. UV Carry Lite does not carry a texture into another material's images, and does not combine materials.
- Carried images change in memory, as texture painting changes them. Ctrl+Z undoes a carry; Save Carried Images keeps it on disk, after a backup of each file.
- A carry over the Memory Budget in the preferences (4 GB by default) is refused before anything is written.
- UV Carry Lite does not carry normal maps. While a normal map is in the Images list (N > UV Carry), it refuses to carry; leave it out there to carry the other images, and the normal map stays as it was.

## Page

https://github.com/ghbanck/UV-Carry-Lite - UV Carry Lite's page, its source and its releases.

## License

Copyright (C) 2026 Gustavo Banck. UV Carry Lite is free, licensed under the GNU General Public License, version 3 or any later version: see `LICENSE`.

The GPL covers the code, not the names: "UV Carry", "UV Carry Lite" and the UV Carry logo are the author's. A modified or redistributed copy keeps every right the GPL gives, but must not be called by these names or carry the logo.
