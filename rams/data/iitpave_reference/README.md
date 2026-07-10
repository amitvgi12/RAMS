# IITPAVE reference I/O

Ground-truth inputs and outputs from the official **IITPAVE** program (the IRC:37-2018
layered-elastic analysis tool, distributed by IRC/IIT as a Windows Fortran binary,
extracted from `IRC_37_IITPAVE.rar`). These pin the pure-Python re-implementation in
[`rams/iitpave_engine.py`](../../iitpave_engine.py); see
[`tests/test_iitpave_engine.py`](../../../tests/test_iitpave_engine.py).

Each `.out` reports, per `(Z, R)` point, the stress tensor (`SigmaZ SigmaT SigmaR
TaoRZ`), vertical deflection (`DispZ`) and the three normal strains (`epZ epT epR`).
A trailing `L` on `Z` means "just below the interface" (the lower layer's side).

| file | layers | profile | used as ground truth |
|------|--------|---------|----------------------|
| `case_a.out` | 3 | 2000 / 200 / 75 MPa (monotone) | **yes** |
| `case_b.out` (+ `case_b.in`) | 3 | 15000 / 125 / 66 MPa (stiff top) | **yes** |
| `case_c.out` (+ `case_c.in`) | 7 | non-monotone, incl. E=5000 buried | **no** — see below |

The engine reproduces `case_a` and `case_b` row-for-row to ~0.1%.

`case_c.out` is **not** used as a reference: its tabulated values are internally
inconsistent — at `Z=700` the stresses, deflection and horizontal strains are
*discontinuous* across a bonded interface (compare the `700.00` and `700.00L` rows),
which no valid linear-elastic solution permits. It appears to be a stale/experimental
dump (the source `.rar` contained many scratch files). It is kept only for provenance.

`gauss.qua` is IITPAVE's Gauss–Legendre quadrature table (10/6/4-point) for the Hankel
integration; the engine hard-codes the equivalent 8-point rule, so this is reference
only.
