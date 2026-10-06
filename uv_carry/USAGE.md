# UV Carry - Usage

UV Carry Lite runs this workflow in Blender 5.1, from the UV editor.

## Workflow

1. In the 3D View, select the mesh and press Tab for Edit Mode: the UV editor shows its UVs. Pick the texture in the UV editor's image menu to see it under them.
2. Turn UV Carry on: the UV Carry button in the UV editor's header, or N > UV Carry > On.
3. Select one or more complete UV islands: L over each, or A for all.
4. Review the images the carry will write in the Images list of the UV Carry panel (N > UV Carry): every image the islands' materials reach, what UV Carry makes of it and whether it is carried, with a checkbox to leave an image out or include an optional one. Each island carries the images of its own materials.
5. Start moving, rotating or scaling the islands.
6. Perform one or more supported transforms. Intermediate transforms change UVs only.
7. Press `Ctrl+Enter` after the final transform is complete.
8. UV Carry checks the islands and carries each one's texels from where it started to where it ended, in every image of the set.
9. Images change in memory, as texture painting changes them; Ctrl+Z undoes the carry.
10. To keep the images on disk, use Save Carried Images in the UV Carry panel. It reports each image's save apart from the carry.

## Several islands

Every selected island must be complete: a move with part of an island selected opens no session. The islands move together under one G, R or S; with the Individual Origins pivot, R and S turn and scale each island about its own centre. Each island is carried under its own map. If one island cannot be carried (it was mirrored, or it would leave the image tile), nothing is written and every island goes back; the message names the island. Where islands land on each other, interiors win over margins and a later island over an earlier one, and the message reports those texels.

## Padding without a move

With complete islands selected (all of them, for example) and no move, `Ctrl+Enter` pads them where they are, by Margin (px), in every image of the set. Only texels that no UV of the mesh uses are filled, from the islands' own texels, ring by ring; between two islands a texel takes the nearer one, and padding never spreads past another UV. A session whose islands are moved back where it started pads them too. Ctrl+Z undoes a padding; Save Carried Images saves it.

## Cancellation

- `Esc` during an active transform cancels only that transform.
- `Cancel Session` restores the session origin.
- `Ctrl+Enter` pressed while a transform is active only confirms that transform, as Blender does; press `Ctrl+Enter` again to commit.

## Saving

The UV Carry panel lists the images carries changed that are unsaved, with Save Carried Images:

- an image from a file is saved over that file in its own format, as Image > Save does, after a copy of the file is kept as a backup in the extension's user folder (the newest five per file);
- a packed image is packed again from memory; it reaches the disk when the .blend is saved;
- a generated image has no file and stays listed: save it with Image > Save As.

A save that fails names the image and the reason. The file on disk stays as it was and the image stays listed. Undo does not change files already saved: after Ctrl+Z, save again to bring the file in line with memory.

## Islands that land on other UVs

UV Carry does not look for free UV space or move other UVs. If an island lands where another UV already uses the texture, the carried texels are written there, and the message says how many texels of other UVs were overwritten. A padding without a move never writes a texel another UV uses.

## Not supported

- UV Sync Selection on: turn it off, or no session opens;
- part of an island selected;
- UDIM tiles, and islands outside the 0 to 1 tile;
- mirrored islands, and transforms that collapse an island;
- shear, relax, unwrap and other deformations of an island;
- procedural textures: bake them to images first;
- material setups UV Carry does not read: the message names the material and the input.
- normal maps, in tangent or object space: leave a normal map out in the Images list to carry the other images;
- carrying a texture into another material's images, or combining materials.
