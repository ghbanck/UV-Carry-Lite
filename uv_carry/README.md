# UV Carry Lite 0.4.1

UV Carry Lite is a Blender 5.1 add-on. Move, rotate or scale complete UV islands, press Ctrl+Enter, and the texture under them follows them, in every image of their materials.

This is a pre-release. It was run with Blender 5.1.1 on Windows 11; other Blender versions and operating systems are not verified.

## Install

1. In Blender 5.1, open Edit > Preferences > Get Extensions.
2. Open the menu at the top right, choose Install from Disk, and pick this zip. Dropping the zip on the Blender window does the same.
3. UV Carry appears in the UV editor's header. Its preferences (memory budget, undo memory, trace folder) are under Edit > Preferences > Add-ons > UV Carry Lite.

To remove it: Edit > Preferences > Add-ons > UV Carry Lite, open its menu and choose Uninstall.

## Quick start

1. In the UV editor, press UV Carry in the header, or N > UV Carry > On.
2. Select complete UV islands: L over each, or A for all of them.
3. Move, rotate or scale them with G, R and S.
4. Press Ctrl+Enter over the UV editor: the texture follows the islands.
5. Save Carried Images, in the UV Carry panel, writes the changed images to disk.

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

Copyright (C) 2026 Gustavo Banck. UV Carry Lite is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version (`LICENSE`). It is distributed in the hope that it will be useful, but without any warranty.
