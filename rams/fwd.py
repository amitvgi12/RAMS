"""
FWD / Benkelman-beam survey processing: deflection -> structural number (SNP).

A raw deflection survey gives a rebound deflection per chainage, but the HDM-4
rut model and the residual-life calculation want the *adjusted structural number*
SNP. This module back-calculates SNP from deflection so a bare FWD/Benkelman
survey can drive the structural models without a separate SN computation.

Relationship (HDM-4 / Paterson-style, monotonic -- weaker pavement deflects more,
so has a lower structural number):

    SNP = a * DEF^(-b)

`a`, `b` are illustrative defaults seeded to commonly-cited HDM-4 values
(SNP ~= 5.0 at DEF 0.5 mm, ~3.2 at 1.0 mm, ~2.5 at 1.5 mm). They are NOT a
substitute for an agency back-calculation against layer moduli / DCP -- override
`DeflectionToSNP` with your calibrated relationship before production use.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import DEFAULT_BOUNDS, InputBounds


@dataclass(frozen=True)
class DeflectionToSNP:
    """Back-calculate an adjusted structural number from rebound deflection.

    SNP = coeff_a * deflection_mm ** (-coeff_b), clamped to the engine's SNP
    bounds so a degenerate deflection can't produce an out-of-range structure.
    """

    coeff_a: float = 3.2
    coeff_b: float = 0.63

    def snp(self, deflection_mm: float, bounds: InputBounds = DEFAULT_BOUNDS) -> float:
        d = max(1e-6, float(deflection_mm))
        value = self.coeff_a * math.pow(d, -self.coeff_b)
        return round(max(bounds.snp_min, min(bounds.snp_max, value)), 3)


DEFAULT_DEFLECTION_TO_SNP = DeflectionToSNP()


def snp_from_deflection(
    deflection_mm: float,
    *,
    model: DeflectionToSNP = DEFAULT_DEFLECTION_TO_SNP,
    bounds: InputBounds = DEFAULT_BOUNDS,
) -> float:
    """Convenience wrapper: rebound deflection (mm) -> adjusted structural number."""
    return model.snp(deflection_mm, bounds)
