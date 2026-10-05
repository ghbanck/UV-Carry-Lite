# UV Carry - Usage

UV Carry Lite runs this workflow in Blender 5.1, from the UV editor.

## Workflow

1. Turn UV Carry on: the UV Carry button in the UV editor's header, or N > UV Carry > On.
2. Select one or more complete UV islands: L over each, or A for all.
3. Review the images the carry will write in the Images list of the UV Carry panel (N > UV Carry): every image the islands' materials reach, what UV Carry makes of it and whether it is carried, with a checkbox to leave an image out or include an optional one. Each island carries the images of its own materials.
4. Start moving, rotating or scaling the islands.
5. Perform one or more supported transforms. Intermediate transforms change UVs only.
6. Press `Ctrl+Enter` after the final transform is complete.
7. UV Carry validates the final context and plans one transfer per island from the original session position directly to the final position.
8. The tool applies the coordinated in-memory result. Images change in memory only, as texture painting changes them; Ctrl+Z undoes the carry.
9. To keep the images on disk, use Save Carried Images in the UV Carry panel. It reports each image's save apart from the carry.

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

## D1

UV Carry does not search for free UV space or move neighboring UVs. If the destination texture region is already used by another UV, the written pixels may affect that other surface. This is accepted MVP behavior and should be visible to the user. A padding without a move never writes a texel another UV uses.

## Unsupported MVP cases

- UV Sync Selection enabled (the session does not open);
- partial island selection;
- UDIM / outside-tile operation;
- reflection / mirrored transform;
- singular or numerically unstable transform;
- explicit shear tool;
- relax/unwrap/local deformation;
- procedural bake conversion;
- unsupported or ambiguous material mapping;
- normal maps, in tangent or object space: UV Carry Pro carries them. A normal map in the Images list refuses the carry; leave it out there to carry the other images;
- carrying a texture into another material's images, or combining materials: UV Carry Pro does it.
