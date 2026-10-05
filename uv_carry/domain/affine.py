"""One affine map from an island's origin UVs to its final UVs.

The MVP carries a composition of moves, rotations and positive scales: one affine map, fitted to
every corner of the island. Before any write it is refused when it does not reproduce the corners
within FIT_RESIDUAL_MAX, mirrors the island, or is near singular or badly conditioned.
The thresholds are provisional.
"""

import math
from dataclasses import dataclass

import numpy as np

FIT_RESIDUAL_MAX = 1e-5     # UV units; largest distance between a mapped origin corner and its final UV
FIT_MIN_SCALE = 1e-4        # smallest singular value accepted for the linear part
FIT_MAX_CONDITION = 1e4     # largest ratio between the singular values

REFUSALS = {
    "degenerate_source": "Island has no area at its origin",
    "residual": "Island was reshaped, not only moved, rotated or scaled",
    "reflection": "Island was mirrored; mirroring is outside the MVP",
    "near_singular": "Island was scaled almost to nothing",
    "condition": "Island was stretched too far in one direction",
}


@dataclass(frozen=True, eq=False)
class AffineFit:
    matrix: np.ndarray            # (2, 2) linear part in UV units: final = matrix @ origin + offset
    offset: np.ndarray            # (2,)
    residual_max: float           # UV units
    det: float
    singular_values: np.ndarray   # (2,), largest first
    verdict: str                  # "ok" or a key of REFUSALS

    @property
    def ok(self):
        return self.verdict == "ok"

    @property
    def rotation_deg(self):
        """Rotation of the polar decomposition, in degrees, counter-clockwise."""
        m = self.matrix
        return math.degrees(math.atan2(m[1, 0] - m[0, 1], m[0, 0] + m[1, 1]))

    def pixel_map(self, width, height):
        """(matrix, offset) of the same map between texel coordinates, in which texel (i, j) of a
        width x height image sits at (i, j): its center is ((i + 0.5) / width, (j + 0.5) / height)."""
        s = np.diag([float(width), float(height)])
        m = s @ self.matrix @ np.linalg.inv(s)
        c = np.array([0.5, 0.5])
        return m, m @ c + s @ self.offset - c


def fit_affine(origin, final):
    """Least-squares affine map from origin to final corner UVs, (N, 2) each, with its verdict."""
    src = np.asarray(origin, np.float64).reshape(-1, 2)
    dst = np.asarray(final, np.float64).reshape(-1, 2)
    a = np.hstack([src, np.ones((len(src), 1))])
    p = np.linalg.lstsq(a, dst, rcond=None)[0]
    m, t = p[:2].T, p[2]
    residual = float(np.linalg.norm(a @ p - dst, axis=1).max()) if len(src) else 0.0
    det = float(np.linalg.det(m))
    sv = np.linalg.svd(m, compute_uv=False)
    if len(src) < 3 or np.linalg.matrix_rank(a, tol=1e-9) < 3:
        verdict = "degenerate_source"
    elif residual > FIT_RESIDUAL_MAX:
        verdict = "residual"
    elif det <= 0:
        verdict = "reflection"
    elif sv[1] < FIT_MIN_SCALE:
        verdict = "near_singular"
    elif sv[0] / sv[1] > FIT_MAX_CONDITION:
        verdict = "condition"
    else:
        verdict = "ok"
    return AffineFit(m, t, residual, det, sv, verdict)
