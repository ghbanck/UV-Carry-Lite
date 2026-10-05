# UV Carry Lite

**Move UVs. Carry Textures.**

A free Blender 5.1 add-on: move, rotate or scale complete UV islands with Blender's own G, R and S, press `Ctrl+Enter`, and the texture under them follows them, in every image of their materials.

<p align="center">
  <img src="assets/demo/uv-carry-demo.png" alt="UV Carry tool mode in Blender 5.1: the UV island of Suzanne's eye is moved with G, Ctrl+Enter carries its texels, and the eye on the model looks as it did before" width="100%">
  <br>
  <a href="assets/demo/uv-carry-demo.mp4"><b>▶ Watch the demo</b></a> (12 seconds, MP4)
</p>

<sub>The author's screen recording in Blender 5.1.1: G moves the island of Suzanne's eye, Ctrl+Enter carries its texels, and Object Mode shows the eye as it was.</sub>

- **Move, rotate or scale.** G, R and S stay as they are. Press `Ctrl+Enter` when the islands are where you want them, and their texels follow, in every image of their materials: colour, packed channels, alpha.
- **Several islands at once**, each under its own move; `Ctrl+Enter` with no move pads the selected islands instead, filling only texels no UV uses.
- **Undo and save.** `Ctrl+Z` restores texels and UVs together, and Save Carried Images writes the changed images, after a backup of each file.
- **Nothing silent.** Mirrored, collapsing and off-tile islands, and material setups UV Carry Lite does not carry, are refused before anything is written, with a message that says why.

## Download and install

UV Carry Lite needs Blender 5.1.

1. Download `uv_carry-<version>.zip` from the [Releases](https://github.com/ghbanck/UV-Carry-Lite/releases).
2. In Blender, open Edit > Preferences > Get Extensions, open the menu at the top right, choose Install from Disk and pick the zip. Dropping the zip on Blender's window does the same.
3. UV Carry appears in the UV editor's header.

The zip holds the add-on, a short README and the usage guide.

## Quick start

1. In the UV editor, press **UV Carry** in the header (or N > UV Carry > On).
2. Select complete UV islands (L over each, or A for all).
3. Move, rotate or scale them with G, R and S.
4. Press `Ctrl+Enter` over the UV editor: the texture follows the islands.
5. Save Carried Images, in the UV Carry panel, writes the changed images to disk.

## What UV Carry Lite does not do

- **Normal maps.** UV Carry Pro carries them. While a normal map is in the Images list of the UV Carry panel, UV Carry Lite refuses to carry; leave it out there to carry the other images, and the normal map stays as it was.
- **Other materials' images.** Each island carries the images of its own materials. Carrying islands into another material's images, which merges several materials into one atlas, is UV Carry Pro's.
- **Pack Islands.** UV Carry Pro carries the texture of islands packed with UV > Pack Islands.
- **UDIM tiles, mirrored islands and parts of islands** are refused.

## How it works

UV Carry writes no pixel while the islands move. It remembers where they started, lets Blender move them as it always does, and does the texture work once, at `Ctrl+Enter`.

![How a carry works: the island's texels are frozen at its origin; at its final place every covered texel centre is mapped back and read from the four nearest frozen texels; in UV Carry Pro, normal maps are re-expressed in the surface frame](assets/how/how-it-works.svg)

1. **Corners, read as arrays.** Every face corner of the mesh joins a vertex, with its position x, y, z in 3D, to a UV, u, v in the image tile [0, 1]. UV Carry reads the corners' UVs, their vertices, the UV selection and the face materials as whole arrays.
2. **Islands and their origin.** Two faces belong to one island when they share an edge with the same UVs at both ends. A move of complete islands opens a session. The islands' UVs before the move are its origin, and the texels under each island are copied ("frozen") from the images its own materials use. If the mesh or the images change, the session ends and the islands go back.
3. **One map per island.** At `Ctrl+Enter`, each island's map from its origin UVs to its final UVs is fitted to its corners by least squares: `[u', v'] = A [u, v] + t`, with `A` a 2 x 2 matrix. A move alone by whole texels is copied bit for bit; rotations and scales are resampled. A mirror (`det A < 0`), a collapse or an island whose shape changed is refused before any write.
4. **From UVs to texels.** An image of W x H texels covers the tile, and texel (i, j) has its centre at ((i + 0.5)/W, (j + 0.5)/H). A texel belongs to an island when its centre lies inside one of the island's UV triangles; an island thinner than a texel takes the texel under its centroid. Every image keeps its own resolution.
5. **Backwards, never forwards.** Every texel the island covers at its final place is mapped back by the inverse map into the frozen source and read bilinearly from the island's own texels only, so nothing bleeds in from a neighbouring island. Margin (px) rings are then grown around the written texels from their own edge values: no gap, no seam.
6. **Normal maps follow their surface (UV Carry Pro).** A tangent-space normal map stores directions in each surface point's frame. When an island turns or stretches, UV Carry Pro decodes each texel's vector in the frame of its surface point at the origin and encodes it again in the frame of the same point at its final UVs.
7. **One transaction.** Every image's result is computed before the first write. Images are then written one by one, each checked again just before its write; a failure restores what was written and puts the islands back. `Ctrl+Z` restores the texels with the UVs.

## UV Carry Pro

UV Carry Pro adds normal maps, re-expressing tangent-space ones when an island turns; Carry Into, which carries islands into another material's images and merges the islands of several materials into one atlas, channel by channel; and Pack Islands, whose packed islands carry their texture. Installing it replaces UV Carry Lite.

## State

0.4.0 is a pre-release, run with Blender 5.1.1 on Windows 11. Other Blender versions and operating systems are not verified.

## License

Copyright (C) 2026 Gustavo Banck. All rights reserved.

UV Carry Lite is free to download, install and use, for personal or commercial work, under the [UV Carry Lite License](LICENSE). You may not modify it, redistribute it or sell it, in whole or in part; to share it, point people to this page. What you make with it is yours.

The source of UV Carry Lite is private: this repository holds its page, its license and its releases.
