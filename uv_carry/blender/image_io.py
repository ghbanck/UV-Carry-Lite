"""Blender adapter: regional image reads and writes.

Pixels are read and written as stored by Blender (no colour conversion), as float32 with row 0 at
the bottom of the image. Blender moves Image.pixels whole: a slice is read by first copying every
pixel, and a slice assignment writes every pixel back. So a region is read through one foreach_get
of the whole image into a float32 array, and written by patching that array and handing it back with
one foreach_set, both in C. That copy lives only for the call; what the
tool keeps is the region. Byte counters record what each call moved.
"""

import numpy as np

COUNTERS = {"bytes_read": 0, "bytes_written": 0, "largest_copy": 0}


def size(img):
    """(width, height, channels)."""
    return int(img.size[0]), int(img.size[1]), int(img.channels)


def pixels(img):
    """Every texel of `img` as float32, shape (height, width, channels): one copy, for the caller."""
    w, h, c = size(img)
    texels = np.empty(w * h * c, np.float32)
    img.pixels.foreach_get(texels)
    COUNTERS["bytes_read"] += texels.nbytes
    COUNTERS["largest_copy"] = max(COUNTERS["largest_copy"], texels.nbytes)
    return texels.reshape(h, w, c)


def store(img, texels):
    """Hand every texel back to `img` (an array from pixels(), patched) and tag the image as changed."""
    img.pixels.foreach_set(np.ascontiguousarray(texels, np.float32).ravel())
    COUNTERS["bytes_written"] += texels.nbytes
    img.update()


def read_region(img, region):
    """Copy of the texels of `region` (half-open), shape (height, width, channels)."""
    if region.is_empty:
        return np.zeros((0, 0, size(img)[2]), np.float32)
    return pixels(img)[region.y0:region.y1, region.x0:region.x1].copy()


def write_region(img, region, patch):
    """Write `patch` (height, width, channels) into `region` and tag the image as changed."""
    if region.is_empty:
        return
    texels = pixels(img)
    texels[region.y0:region.y1, region.x0:region.x1] = patch
    store(img, texels)
