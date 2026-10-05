"""UV islands over face corners.

Two faces belong to the same island when they share a mesh edge and the UV coordinates at both
ends of that edge coincide within an explicit tolerance.
"""

from dataclasses import dataclass

import numpy as np

UV_CONNECT_EPS = 1e-6   # UV units; provisional until measured


@dataclass(frozen=True, eq=False)
class FaceCorners:
    """Face-corner view of a mesh. Corners of face f are face_start[f] .. face_start[f] + face_len[f] - 1."""

    face_start: np.ndarray
    face_len: np.ndarray
    corner_vert: np.ndarray
    uv: np.ndarray

    @property
    def corner_face(self):
        return np.repeat(np.arange(len(self.face_len)), self.face_len)


@dataclass(frozen=True, eq=False)
class Selection:
    """Outcome of checking a UV selection against the islands."""

    kind: str                 # "ok", "empty", "multiple" or "partial"
    island: int = -1          # the first island id when kind == "ok"
    detail: dict = None
    islands: tuple = ()       # every island id when kind == "ok", ascending


def resolve_islands(corners, face_mask=None, eps=UV_CONNECT_EPS):
    """Island id per face; -1 for faces outside face_mask. Ids follow first appearance in face order.

    Vectorized: every edge of a shown face is a half-edge (its face, the corner at its lower vertex,
    the corner at its higher vertex). Half-edges sorted by their two vertices fall into one group per
    mesh edge; two of a group join their faces when the UVs at both ends coincide."""
    n = len(corners.face_start)
    ids = np.full(n, -1, np.int64)
    shown = np.ones(n, bool) if face_mask is None else np.asarray(face_mask, bool)
    face_len = np.asarray(corners.face_len, np.int64)
    face_of = np.repeat(np.arange(n), face_len)
    face_of = face_of[shown[face_of]]
    if not len(face_of):
        return ids
    first = np.asarray(corners.face_start, np.int64)[face_of]
    count = face_len[face_of]
    offset = np.arange(len(face_of)) - np.repeat(np.cumsum(face_len[shown]) - face_len[shown], face_len[shown])
    k0, k1 = first + offset, first + (offset + 1) % count
    vert = np.asarray(corners.corner_vert, np.int64)
    v0, v1 = vert[k0], vert[k1]
    lo, hi = np.where(v0 < v1, k0, k1), np.where(v0 < v1, k1, k0)
    a, b = np.minimum(v0, v1), np.maximum(v0, v1)
    order = np.lexsort((b, a))
    a, b = a[order], b[order]
    pi, pj = _pairs_in_groups(a, b)
    pi, pj = order[pi], order[pj]
    uv = np.asarray(corners.uv)
    join = (np.abs(uv[lo[pi]] - uv[lo[pj]]) <= eps).all(axis=1) & (np.abs(uv[hi[pi]] - uv[hi[pj]]) <= eps).all(axis=1)
    roots = _components(n, face_of[pi[join]], face_of[pj[join]])[shown]
    unique, first_seen = np.unique(roots, return_index=True)
    rank = np.empty(len(unique), np.int64)
    rank[np.argsort(first_seen)] = np.arange(len(unique))
    ids[shown] = rank[np.searchsorted(unique, roots)]
    return ids


def _pairs_in_groups(a, b):
    """Every pair (i, j), i < j, of positions with the same key (a, b), the keys sorted."""
    same = (a[1:] == a[:-1]) & (b[1:] == b[:-1])
    starts = np.flatnonzero(np.concatenate(([True], ~same)))
    sizes = np.diff(np.append(starts, len(a)))
    pi, pj = [np.flatnonzero(same)], [np.flatnonzero(same) + 1]
    if len(sizes) and sizes.max() > 2:          # non-manifold edges: farther pairs, in those groups only
        big = np.flatnonzero(np.repeat(sizes > 2, sizes))
        ba, bb = a[big], b[big]
        for d in range(2, int(sizes.max())):
            q = np.flatnonzero((ba[d:] == ba[:-d]) & (bb[d:] == bb[:-d]))
            pi.append(big[q])
            pj.append(big[q + d])
    return np.concatenate(pi), np.concatenate(pj)


def _components(n, a, b):
    """For n nodes joined by edges a-b, the smallest node of each node's component."""
    labels = np.arange(n)
    while len(a):
        la, lb = labels[a], labels[b]
        apart = la != lb
        if not apart.any():
            break
        a, b, la, lb = a[apart], b[apart], la[apart], lb[apart]
        np.minimum.at(labels, np.maximum(la, lb), np.minimum(la, lb))     # hook roots to smaller roots
        while True:                                                         # then point every node at its root
            up = labels[labels]
            if np.array_equal(up, labels):
                break
            labels = up
    return labels


def classify_selection(island_of_face, corner_face, selected, several=False):
    """Complete islands selected, or why not: "ok" for exactly one complete island, or with `several` for one
    or more, every one complete; "empty"; "multiple" for several islands without
    `several`; "partial" when an island is only partly selected. Corners of faces with id -1 are ignored."""
    island_of_corner = np.asarray(island_of_face)[corner_face]
    shown = island_of_corner >= 0
    sel = np.asarray(selected, bool) & shown
    touched = np.unique(island_of_corner[sel])
    if not len(touched):
        return Selection("empty")
    if len(touched) > 1 and not several:
        return Selection("multiple", detail={"islands": touched.tolist()})
    count = int(island_of_corner.max()) + 1
    missing = np.bincount(island_of_corner[shown & ~sel], minlength=count)[touched]
    members = np.bincount(island_of_corner[shown], minlength=count)[touched]
    partial = missing > 0
    if partial.any():
        return Selection("partial", detail={"island": int(touched[partial][0]),
                                            "unselected_corners": int(missing.sum()),
                                            "corners": int(members[partial].sum()),
                                            "islands": touched[partial].tolist()})
    return Selection("ok", island=int(touched[0]), islands=tuple(touched.tolist()))


def island_corners(island_of_face, corner_face, island):
    """Corner indices of one island, in corner order."""
    return np.flatnonzero(np.asarray(island_of_face)[corner_face] == island)
