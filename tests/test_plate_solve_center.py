"""_read_solution's field-centre pixel index.

astropy's WCS.pixel_to_world takes 0-indexed pixel coordinates, so the
correct centre-pixel index for an nx-wide image is (nx-1)/2, not nx/2
(alpaca/platesolve.py already uses the correct convention; this pins
plate_solve.py's _read_solution to the same one).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astropy.io import fits

from src.plate_solve import _read_solution

NX, NY = 100, 80
CRVAL1, CRVAL2 = 123.456, 45.678


def _write_wcs(path: Path) -> None:
    hdr = fits.Header()
    hdr["IMAGEW"] = NX
    hdr["IMAGEH"] = NY
    hdr["CTYPE1"] = "RA---TAN"
    hdr["CTYPE2"] = "DEC--TAN"
    # CRPIX is 1-indexed; placing it at the FITS-convention centre of the
    # image means the 0-indexed centre pixel is exactly (CRPIX - 1) and
    # must reproduce CRVAL exactly, regardless of image parity.
    hdr["CRPIX1"] = (NX + 1) / 2.0
    hdr["CRPIX2"] = (NY + 1) / 2.0
    hdr["CRVAL1"] = CRVAL1
    hdr["CRVAL2"] = CRVAL2
    hdr["CD1_1"] = -1.0 / 3600.0
    hdr["CD1_2"] = 0.0
    hdr["CD2_1"] = 0.0
    hdr["CD2_2"] = 1.0 / 3600.0
    fits.PrimaryHDU(header=hdr).writeto(path, overwrite=True)


class ReadSolutionCenterTest(unittest.TestCase):
    def test_centre_pixel_reproduces_crval_exactly(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "solved.wcs"
            _write_wcs(path)
            out = _read_solution(str(path))
        self.assertAlmostEqual(out["ra_deg"], CRVAL1, places=9)
        self.assertAlmostEqual(out["dec_deg"], CRVAL2, places=9)


if __name__ == "__main__":
    unittest.main()
