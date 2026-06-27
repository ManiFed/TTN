#!/usr/bin/env python3
"""
Telescope spec catalog — the single source of truth for telescope hardware,
shared by both the Node Agent and The Telescope Net cloud.

Like ``shared_models``, this module imports nothing heavy (no astropy, no
Flask) so both sides can import it for free.  It does three jobs:

  1. ``CATALOG``        — known telescope models and their physical specs
  2. ``derive_params``  — turn physical specs into the photometry/scheduler
                          parameters the pipeline actually consumes
  3. ``enrich_config_with_telescope`` / ``detect_telescope_specs``
                        — populate a node config from a model name (mirrors
                          ``geolocation.enrich_config_with_location``) or from
                          a live ALPACA device (works for *any* equipment).

Design note on anchors
----------------------
Every derivation is anchored so the **ZWO Seestar S50** reproduces the values
the pipeline used when it was the only supported scope (pixel_scale 2.4″/px,
fov 1.27°, mag_min 10.0, mag_limit 15.0, field_radius 0.5°, fwhm fallback
4.0 px).  Larger apertures, faster optics, finer pixels and cooled sensors all
scale away from that anchor by the physics below.
"""

import logging
import math
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

logger = logging.getLogger("telescope_specs")

ARCSEC_PER_RADIAN = 206264.806  # 206.265″ per mrad → arcsec = 206.265 × µm / mm


# ── Spec model ──────────────────────────────────────────────────────────────────

@dataclass
class TelescopeSpec:
    """
    Physical description of one telescope + camera combination.

    These are the *measured* hardware facts; everything the algorithm needs
    (pixel scale, field of view, magnitude limits, …) is computed from them by
    ``derive_params`` so there is exactly one place that knows the physics.
    """
    display_name: str = ""
    aliases: list = field(default_factory=list)   # alternate spellings users may type

    # Optics
    aperture_mm: float = 0.0
    focal_length_mm: float = 0.0
    mount_type: str = "alt_az"        # alt_az | equatorial

    # Sensor
    pixel_size_um: float = 0.0
    sensor_w_px: int = 0
    sensor_h_px: int = 0
    sensor_name: str = ""
    cooled: bool = False
    gain_e_per_adu: float = 1.0
    read_noise_e: float = 5.0

    # Operational
    max_exposure_s: float = 30.0      # alt-az field-rotation limit; long for EQ
    default_filters: list = field(default_factory=lambda: ["CV"])
    tier: int = 1                     # 1=smartscope/broadband 2=filtered 3=spectroscopy
    camera_model: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TelescopeSpec":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


# ── Catalog ─────────────────────────────────────────────────────────────────────
# Specs are nominal manufacturer figures; when a node is online the live ALPACA
# device (``detect_telescope_specs``) overrides whatever it can actually report.

CATALOG: dict[str, TelescopeSpec] = {
    "seestar_s50": TelescopeSpec(
        display_name="ZWO Seestar S50",
        aliases=["seestar s50", "seestar", "s50", "zwo seestar s50"],
        aperture_mm=50.0, focal_length_mm=250.0, mount_type="alt_az",
        pixel_size_um=2.9, sensor_w_px=1920, sensor_h_px=1080,
        sensor_name="IMX462", camera_model="ZWO Seestar S50 IMX462",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "seestar_s30": TelescopeSpec(
        display_name="ZWO Seestar S30",
        aliases=["seestar s30", "s30", "seestar s30 pro", "s30 pro", "zwo seestar s30"],
        aperture_mm=30.0, focal_length_mm=150.0, mount_type="alt_az",
        pixel_size_um=2.9, sensor_w_px=1920, sensor_h_px=1080,
        sensor_name="IMX662", camera_model="ZWO Seestar S30 IMX662",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "vespera": TelescopeSpec(
        display_name="Vaonis Vespera",
        aliases=["vaonis vespera", "vespera classic"],
        aperture_mm=50.0, focal_length_mm=200.0, mount_type="alt_az",
        pixel_size_um=2.9, sensor_w_px=1920, sensor_h_px=1080,
        sensor_name="IMX462", camera_model="Vaonis Vespera IMX462",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "vespera_ii": TelescopeSpec(
        display_name="Vaonis Vespera II",
        aliases=["vaonis vespera ii", "vespera 2", "vespera2"],
        aperture_mm=50.0, focal_length_mm=250.0, mount_type="alt_az",
        pixel_size_um=2.9, sensor_w_px=3840, sensor_h_px=2160,
        sensor_name="IMX585", camera_model="Vaonis Vespera II IMX585",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "vespera_pro": TelescopeSpec(
        display_name="Vaonis Vespera Pro",
        aliases=["vaonis vespera pro", "vespera pro"],
        aperture_mm=50.0, focal_length_mm=250.0, mount_type="alt_az",
        pixel_size_um=2.0, sensor_w_px=3552, sensor_h_px=3552,
        sensor_name="IMX676", camera_model="Vaonis Vespera Pro IMX676",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "stellina": TelescopeSpec(
        display_name="Vaonis Stellina",
        aliases=["vaonis stellina"],
        aperture_mm=80.0, focal_length_mm=400.0, mount_type="alt_az",
        pixel_size_um=2.4, sensor_w_px=3096, sensor_h_px=2080,
        sensor_name="IMX178", camera_model="Vaonis Stellina IMX178",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "dwarf_ii": TelescopeSpec(
        display_name="DwarfLab Dwarf II",
        aliases=["dwarf ii", "dwarf 2", "dwarf2", "dwarflab dwarf ii"],
        aperture_mm=24.0, focal_length_mm=100.0, mount_type="alt_az",
        pixel_size_um=1.45, sensor_w_px=3840, sensor_h_px=2160,
        sensor_name="IMX415", camera_model="Dwarf II IMX415",
        max_exposure_s=15.0, default_filters=["CV"], tier=1,
    ),
    "dwarf_3": TelescopeSpec(
        display_name="DwarfLab Dwarf 3",
        aliases=["dwarf 3", "dwarf iii", "dwarf3", "dwarflab dwarf 3"],
        aperture_mm=35.0, focal_length_mm=150.0, mount_type="alt_az",
        pixel_size_um=2.0, sensor_w_px=3840, sensor_h_px=2160,
        sensor_name="IMX678", camera_model="Dwarf 3 IMX678",
        max_exposure_s=15.0, default_filters=["CV"], tier=1,
    ),
    "evscope_2": TelescopeSpec(
        display_name="Unistellar eVscope 2",
        aliases=["evscope 2", "evscope2", "unistellar evscope 2", "evscope"],
        aperture_mm=114.0, focal_length_mm=450.0, mount_type="alt_az",
        pixel_size_um=2.9, sensor_w_px=3096, sensor_h_px=2080,
        sensor_name="IMX347", camera_model="Unistellar eVscope 2 IMX347",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "equinox_2": TelescopeSpec(
        display_name="Unistellar eQuinox 2",
        aliases=["equinox 2", "equinox2", "unistellar equinox 2", "equinox"],
        aperture_mm=114.0, focal_length_mm=450.0, mount_type="alt_az",
        pixel_size_um=2.9, sensor_w_px=3096, sensor_h_px=2080,
        sensor_name="IMX347", camera_model="Unistellar eQuinox 2 IMX347",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "odyssey": TelescopeSpec(
        display_name="Unistellar Odyssey",
        aliases=["unistellar odyssey", "odyssey pro"],
        aperture_mm=85.0, focal_length_mm=320.0, mount_type="alt_az",
        pixel_size_um=2.0, sensor_w_px=3856, sensor_h_px=2180,
        sensor_name="IMX676", camera_model="Unistellar Odyssey IMX676",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "celestron_origin": TelescopeSpec(
        display_name="Celestron Origin",
        aliases=["celestron origin", "origin"],
        aperture_mm=152.0, focal_length_mm=335.0, mount_type="alt_az",
        pixel_size_um=2.4, sensor_w_px=3096, sensor_h_px=2080,
        sensor_name="IMX178", camera_model="Celestron Origin IMX178",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "vaonis_hyperia": TelescopeSpec(
        display_name="Vaonis Hyperia",
        aliases=["vaonis hyperia", "hyperia"],
        aperture_mm=150.0, focal_length_mm=1050.0, mount_type="alt_az",
        pixel_size_um=3.76, sensor_w_px=9576, sensor_h_px=6388,
        sensor_name="IMX455", camera_model="Vaonis Hyperia IMX455",
        cooled=True, max_exposure_s=60.0, default_filters=["CV"], tier=2,
    ),
    "evscope_1": TelescopeSpec(
        display_name="Unistellar eVscope",
        aliases=["evscope 1", "evscope gen 1", "unistellar evscope 1", "evscope gen1"],
        aperture_mm=114.0, focal_length_mm=450.0, mount_type="alt_az",
        pixel_size_um=3.75, sensor_w_px=1920, sensor_h_px=1080,
        sensor_name="IMX224", camera_model="Unistellar eVscope IMX224",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "equinox_1": TelescopeSpec(
        display_name="Unistellar eQuinox",
        aliases=["equinox 1", "equinox gen 1", "unistellar equinox 1", "equinox gen1"],
        aperture_mm=114.0, focal_length_mm=450.0, mount_type="alt_az",
        pixel_size_um=3.75, sensor_w_px=1920, sensor_h_px=1080,
        sensor_name="IMX224", camera_model="Unistellar eQuinox IMX224",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "dwarf_s": TelescopeSpec(
        display_name="DwarfLab Dwarf Mini",
        aliases=["dwarf mini", "dwarflab dwarf mini", "dwarf s", "dwarfs", "dwarflab dwarf s"],
        aperture_mm=30.0, focal_length_mm=150.0, mount_type="alt_az",
        pixel_size_um=2.9, sensor_w_px=1920, sensor_h_px=1080,
        sensor_name="IMX662", camera_model="Dwarf Mini IMX662",
        max_exposure_s=90.0, default_filters=["CV"], tier=1,
    ),
    "nexstar_evolution_8": TelescopeSpec(
        display_name="Celestron NexStar Evolution 8",
        aliases=["nexstar evolution 8", "nexstar evo 8", "celestron nexstar evolution 8", "nexstar 8"],
        aperture_mm=203.0, focal_length_mm=2032.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "skywatcher_evostar_80ed": TelescopeSpec(
        display_name="Sky-Watcher Evostar 80ED",
        aliases=["evostar 80ed", "evostar 80", "sky-watcher evostar 80ed", "skywatcher evostar 80ed"],
        aperture_mm=80.0, focal_length_mm=600.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "skywatcher_heritage_130p": TelescopeSpec(
        display_name="Sky-Watcher Heritage 130P",
        aliases=["heritage 130p", "heritage 130", "sky-watcher heritage 130p", "skywatcher heritage 130p"],
        aperture_mm=130.0, focal_length_mm=650.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    # ── Traditional SCTs (no built-in camera; sensor fields left 0) ─────────────
    "celestron_c8": TelescopeSpec(
        display_name="Celestron C8",
        aliases=["c8", "celestron c8", "c8 sct"],
        aperture_mm=203.0, focal_length_mm=2032.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "celestron_c9_25": TelescopeSpec(
        display_name="Celestron C9.25",
        aliases=["c9.25", "c9", "celestron c9.25", "celestron c9"],
        aperture_mm=235.0, focal_length_mm=2350.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "celestron_c11": TelescopeSpec(
        display_name="Celestron C11",
        aliases=["c11", "celestron c11", "c11 sct"],
        aperture_mm=279.0, focal_length_mm=2800.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "celestron_edgehd_8": TelescopeSpec(
        display_name="Celestron EdgeHD 8\"",
        aliases=["edgehd 8", "edge hd 8", "celestron edgehd 8", "celestron edge hd 8"],
        aperture_mm=203.0, focal_length_mm=2032.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "celestron_edgehd_11": TelescopeSpec(
        display_name="Celestron EdgeHD 11\"",
        aliases=["edgehd 11", "edge hd 11", "celestron edgehd 11", "celestron edge hd 11"],
        aperture_mm=279.0, focal_length_mm=2800.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "celestron_rasa_8": TelescopeSpec(
        display_name="Celestron RASA 8",
        aliases=["rasa 8", "celestron rasa 8", "rasa8"],
        aperture_mm=203.0, focal_length_mm=400.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "meade_lx200_8": TelescopeSpec(
        display_name="Meade LX200 8\"",
        aliases=["lx200 8", "meade lx200 8", "lx200 8 acf", "meade lx200"],
        aperture_mm=203.0, focal_length_mm=2000.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "meade_lx90_8": TelescopeSpec(
        display_name="Meade LX90 8\"",
        aliases=["lx90 8", "meade lx90 8", "meade lx90"],
        aperture_mm=203.0, focal_length_mm=2000.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=60.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    # ── Refractors ───────────────────────────────────────────────────────────────
    "skywatcher_esprit_100ed": TelescopeSpec(
        display_name="Sky-Watcher Esprit 100ED",
        aliases=["esprit 100ed", "esprit 100", "sky-watcher esprit 100ed", "skywatcher esprit 100ed"],
        aperture_mm=100.0, focal_length_mm=550.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "skywatcher_esprit_120ed": TelescopeSpec(
        display_name="Sky-Watcher Esprit 120ED",
        aliases=["esprit 120ed", "esprit 120", "sky-watcher esprit 120ed", "skywatcher esprit 120ed"],
        aperture_mm=120.0, focal_length_mm=840.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "skywatcher_evostar_100ed": TelescopeSpec(
        display_name="Sky-Watcher Evostar 100ED",
        aliases=["evostar 100ed", "evostar 100", "sky-watcher evostar 100ed", "skywatcher evostar 100ed"],
        aperture_mm=100.0, focal_length_mm=900.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "william_optics_redcat_51": TelescopeSpec(
        display_name="William Optics RedCat 51",
        aliases=["redcat 51", "redcat51", "william optics redcat 51", "wo redcat 51"],
        aperture_mm=51.0, focal_length_mm=250.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "william_optics_gt81": TelescopeSpec(
        display_name="William Optics GT81",
        aliases=["gt81", "william optics gt81", "wo gt81"],
        aperture_mm=81.0, focal_length_mm=478.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "takahashi_fsq85": TelescopeSpec(
        display_name="Takahashi FSQ-85ED",
        aliases=["fsq85", "fsq-85", "takahashi fsq85", "takahashi fsq-85ed"],
        aperture_mm=85.0, focal_length_mm=450.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "takahashi_fsq106": TelescopeSpec(
        display_name="Takahashi FSQ-106EDX",
        aliases=["fsq106", "fsq-106", "takahashi fsq106", "takahashi fsq-106", "fsq106edx"],
        aperture_mm=106.0, focal_length_mm=530.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    # ── Newtonians / Dobsonians ──────────────────────────────────────────────────
    "skywatcher_200pds": TelescopeSpec(
        display_name="Sky-Watcher 200PDS",
        aliases=["200pds", "skywatcher 200pds", "sky-watcher 200pds", "200p ds"],
        aperture_mm=200.0, focal_length_mm=1000.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "skywatcher_150pds": TelescopeSpec(
        display_name="Sky-Watcher 150PDS",
        aliases=["150pds", "skywatcher 150pds", "sky-watcher 150pds"],
        aperture_mm=150.0, focal_length_mm=750.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "skywatcher_dob_10": TelescopeSpec(
        display_name="Sky-Watcher Dobsonian 10\"",
        aliases=["skywatcher dob 10", "sky-watcher dobsonian 10", "dob 10", "10 inch dob"],
        aperture_mm=254.0, focal_length_mm=1200.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    # ── Maks ────────────────────────────────────────────────────────────────────
    "skywatcher_mak_127": TelescopeSpec(
        display_name="Sky-Watcher Mak 127",
        aliases=["mak 127", "mak127", "skywatcher mak 127", "sky-watcher mak 127", "maksutov 127"],
        aperture_mm=127.0, focal_length_mm=1500.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "skywatcher_mak_180": TelescopeSpec(
        display_name="Sky-Watcher Mak 180",
        aliases=["mak 180", "mak180", "skywatcher mak 180", "sky-watcher mak 180", "maksutov 180"],
        aperture_mm=180.0, focal_length_mm=2700.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    # ── More SCTs / astrographs ──────────────────────────────────────────────────
    "celestron_c14": TelescopeSpec(
        display_name="Celestron C14",
        aliases=["c14", "celestron c14", "c14 sct"],
        aperture_mm=356.0, focal_length_mm=3910.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "celestron_edgehd_14": TelescopeSpec(
        display_name="Celestron EdgeHD 14\"",
        aliases=["edgehd 14", "edge hd 14", "celestron edgehd 14"],
        aperture_mm=356.0, focal_length_mm=3910.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "celestron_rasa_11": TelescopeSpec(
        display_name="Celestron RASA 11",
        aliases=["rasa 11", "celestron rasa 11", "rasa11"],
        aperture_mm=279.0, focal_length_mm=620.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "nexstar_5se": TelescopeSpec(
        display_name="Celestron NexStar 5SE",
        aliases=["nexstar 5se", "nexstar 5", "celestron nexstar 5se", "celestron 5se"],
        aperture_mm=127.0, focal_length_mm=1250.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=60.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "nexstar_6se": TelescopeSpec(
        display_name="Celestron NexStar 6SE",
        aliases=["nexstar 6se", "nexstar 6", "celestron nexstar 6se", "celestron 6se"],
        aperture_mm=150.0, focal_length_mm=1500.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=60.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "nexstar_8se": TelescopeSpec(
        display_name="Celestron NexStar 8SE",
        aliases=["nexstar 8se", "celestron nexstar 8se", "celestron 8se"],
        aperture_mm=203.0, focal_length_mm=2032.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=60.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    # ── More Maks ────────────────────────────────────────────────────────────────
    "skywatcher_mak_90": TelescopeSpec(
        display_name="Sky-Watcher Mak 90",
        aliases=["mak 90", "mak90", "skywatcher mak 90", "sky-watcher mak 90", "maksutov 90"],
        aperture_mm=90.0, focal_length_mm=1250.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "skywatcher_mak_102": TelescopeSpec(
        display_name="Sky-Watcher Mak 102",
        aliases=["mak 102", "mak102", "skywatcher mak 102", "sky-watcher mak 102", "maksutov 102"],
        aperture_mm=102.0, focal_length_mm=1300.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    # ── More Newtonians ──────────────────────────────────────────────────────────
    "skywatcher_quattro_8": TelescopeSpec(
        display_name="Sky-Watcher Quattro 8CF",
        aliases=["quattro 8", "quattro 8cf", "skywatcher quattro 8", "sky-watcher quattro 8"],
        aperture_mm=200.0, focal_length_mm=800.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "skywatcher_quattro_10": TelescopeSpec(
        display_name="Sky-Watcher Quattro 10CF",
        aliases=["quattro 10", "quattro 10cf", "skywatcher quattro 10", "sky-watcher quattro 10"],
        aperture_mm=254.0, focal_length_mm=1016.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    # ── More refractors ──────────────────────────────────────────────────────────
    "william_optics_zenithstar_61": TelescopeSpec(
        display_name="William Optics ZenithStar 61",
        aliases=["zenithstar 61", "zs61", "wo zs61", "william optics zs61"],
        aperture_mm=61.0, focal_length_mm=360.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "william_optics_zenithstar_73": TelescopeSpec(
        display_name="William Optics ZenithStar 73",
        aliases=["zenithstar 73", "zs73", "wo zs73", "william optics zs73"],
        aperture_mm=73.0, focal_length_mm=430.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "askar_107phq": TelescopeSpec(
        display_name="Askar 107PHQ",
        aliases=["askar 107phq", "askar 107", "107phq"],
        aperture_mm=107.0, focal_length_mm=749.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "askar_151phq": TelescopeSpec(
        display_name="Askar 151PHQ",
        aliases=["askar 151phq", "askar 151", "151phq"],
        aperture_mm=151.0, focal_length_mm=1057.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "askar_80phq": TelescopeSpec(
        display_name="Askar 80PHQ",
        aliases=["askar 80phq", "askar 80", "80phq"],
        aperture_mm=80.0, focal_length_mm=560.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "televue_np101": TelescopeSpec(
        display_name="TeleVue NP101is",
        aliases=["np101", "televue np101", "np101is", "tele vue np101"],
        aperture_mm=101.0, focal_length_mm=540.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "televue_np127": TelescopeSpec(
        display_name="TeleVue NP127is",
        aliases=["np127", "televue np127", "np127is", "tele vue np127"],
        aperture_mm=127.0, focal_length_mm=660.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "apertura_72ed": TelescopeSpec(
        display_name="Apertura 72ED",
        aliases=["apertura 72ed", "apertura 72", "72ed"],
        aperture_mm=72.0, focal_length_mm=432.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "explore_scientific_ed80": TelescopeSpec(
        display_name="Explore Scientific ED80",
        aliases=["explore scientific ed80", "es ed80", "ed80"],
        aperture_mm=80.0, focal_length_mm=480.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "orion_eon_115ed": TelescopeSpec(
        display_name="Orion EON 115ED",
        aliases=["orion eon 115ed", "eon 115ed", "eon 115"],
        aperture_mm=115.0, focal_length_mm=805.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    # ── Ritchey-Chrétien astrographs ────────────────────────────────────────────
    "gso_rc8": TelescopeSpec(
        display_name="GSO RC 8\"",
        aliases=["gso rc8", "gso rc 8", "rc 8", "rc8", "orion rc8"],
        aperture_mm=203.0, focal_length_mm=1624.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "gso_rc10": TelescopeSpec(
        display_name="GSO RC 10\"",
        aliases=["gso rc10", "gso rc 10", "rc 10", "rc10", "orion rc10"],
        aperture_mm=254.0, focal_length_mm=2000.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "gso_rc12": TelescopeSpec(
        display_name="GSO RC 12\"",
        aliases=["gso rc12", "gso rc 12", "rc 12", "rc12"],
        aperture_mm=305.0, focal_length_mm=2436.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    # ── More SCTs / GOTO ─────────────────────────────────────────────────────────
    "celestron_nexstar_evolution_6": TelescopeSpec(
        display_name="Celestron NexStar Evolution 6",
        aliases=["nexstar evolution 6", "nexstar evo 6", "celestron nexstar evolution 6"],
        aperture_mm=150.0, focal_length_mm=1500.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=60.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "meade_lx200_12": TelescopeSpec(
        display_name="Meade LX200 12\"",
        aliases=["lx200 12", "meade lx200 12", "lx200 12 acf"],
        aperture_mm=305.0, focal_length_mm=3048.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "meade_lx90_10": TelescopeSpec(
        display_name="Meade LX90 10\"",
        aliases=["lx90 10", "meade lx90 10"],
        aperture_mm=254.0, focal_length_mm=2500.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=60.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    # ── More refractors ──────────────────────────────────────────────────────────
    "skywatcher_esprit_150ed": TelescopeSpec(
        display_name="Sky-Watcher Esprit 150ED",
        aliases=["esprit 150ed", "esprit 150", "sky-watcher esprit 150ed", "skywatcher esprit 150ed"],
        aperture_mm=150.0, focal_length_mm=1050.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "skywatcher_evostar_120ed": TelescopeSpec(
        display_name="Sky-Watcher Evostar 120ED",
        aliases=["evostar 120ed", "evostar 120", "sky-watcher evostar 120ed", "skywatcher evostar 120ed"],
        aperture_mm=120.0, focal_length_mm=900.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    "takahashi_tsa120": TelescopeSpec(
        display_name="Takahashi TSA-120",
        aliases=["tsa120", "tsa-120", "takahashi tsa120", "takahashi tsa-120"],
        aperture_mm=120.0, focal_length_mm=900.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "explore_scientific_102ed": TelescopeSpec(
        display_name="Explore Scientific ED102",
        aliases=["explore scientific ed102", "es ed102", "ed102", "explore scientific 102ed"],
        aperture_mm=102.0, focal_length_mm=714.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "explore_scientific_127ed": TelescopeSpec(
        display_name="Explore Scientific ED127",
        aliases=["explore scientific ed127", "es ed127", "ed127", "explore scientific 127ed"],
        aperture_mm=127.0, focal_length_mm=952.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "stellarvue_svx130": TelescopeSpec(
        display_name="Stellarvue SVX130",
        aliases=["svx130", "stellarvue svx130", "stellarvue 130"],
        aperture_mm=130.0, focal_length_mm=910.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "william_optics_fstar_132": TelescopeSpec(
        display_name="William Optics FluoroStar 132",
        aliases=["fluorostar 132", "fstar 132", "wo fstar 132", "william optics fluorostar 132"],
        aperture_mm=132.0, focal_length_mm=925.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "vixen_ed115s": TelescopeSpec(
        display_name="Vixen ED115S",
        aliases=["vixen ed115s", "vixen ed115", "ed115s"],
        aperture_mm=115.0, focal_length_mm=900.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["V", "B", "R", "I"], tier=2,
    ),
    "bresser_messier_ar102": TelescopeSpec(
        display_name="Bresser Messier AR-102",
        aliases=["bresser ar102", "bresser messier ar102", "bresser ar 102"],
        aperture_mm=102.0, focal_length_mm=600.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
    # ── Larger Newtonians / Dobsonians ────────────────────────────────────────────
    "skywatcher_dob_8": TelescopeSpec(
        display_name="Sky-Watcher Dobsonian 8\"",
        aliases=["skywatcher dob 8", "sky-watcher dobsonian 8", "dob 8", "8 inch dob"],
        aperture_mm=203.0, focal_length_mm=1200.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "skywatcher_dob_12": TelescopeSpec(
        display_name="Sky-Watcher Dobsonian 12\"",
        aliases=["skywatcher dob 12", "sky-watcher dobsonian 12", "dob 12", "12 inch dob"],
        aperture_mm=305.0, focal_length_mm=1500.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "apertura_ad8": TelescopeSpec(
        display_name="Apertura AD8 Dobsonian",
        aliases=["apertura ad8", "apertura dob 8", "ad8"],
        aperture_mm=203.0, focal_length_mm=1200.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "apertura_ad10": TelescopeSpec(
        display_name="Apertura AD10 Dobsonian",
        aliases=["apertura ad10", "apertura dob 10", "ad10"],
        aperture_mm=254.0, focal_length_mm=1250.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "orion_xt8": TelescopeSpec(
        display_name="Orion SkyQuest XT8",
        aliases=["orion xt8", "skyquest xt8", "xt8", "orion 8 dob"],
        aperture_mm=203.0, focal_length_mm=1200.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    "orion_xt10": TelescopeSpec(
        display_name="Orion SkyQuest XT10",
        aliases=["orion xt10", "skyquest xt10", "xt10", "orion 10 dob"],
        aperture_mm=254.0, focal_length_mm=1200.0, mount_type="alt_az",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        sensor_name="", camera_model="",
        max_exposure_s=30.0, default_filters=["CV"], tier=1,
    ),
    # Generic template for any OTA + camera on a tracker/mount, or any ALPACA
    # device not in the catalog.  All physical fields are 0 → the operator (app)
    # or the live ALPACA device must supply aperture, focal length and pixel size.
    "custom": TelescopeSpec(
        display_name="Custom / other",
        aliases=["custom", "other", "diy"],
        aperture_mm=0.0, focal_length_mm=0.0, mount_type="equatorial",
        pixel_size_um=0.0, sensor_w_px=0, sensor_h_px=0,
        max_exposure_s=300.0, default_filters=["CV"], tier=1,
    ),
}


# ── Lookup ──────────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


# Pre-index every key, display name and alias to its spec for fuzzy matching.
_INDEX: dict[str, str] = {}
for _key, _spec in CATALOG.items():
    _INDEX[_norm(_key)] = _key
    _INDEX[_norm(_spec.display_name)] = _key
    for _alias in _spec.aliases:
        _INDEX[_norm(_alias)] = _key


def lookup(model: str) -> Optional[TelescopeSpec]:
    """
    Resolve a free-text model name to a catalog spec, case/space-insensitive.

    Tries an exact normalised match first, then a substring match in either
    direction (so "vespera" matches the classic Vespera, and "ZWO Seestar S50
    Smart Telescope" still matches the S50).  Returns None if nothing matches.
    """
    if not model:
        return None
    n = _norm(model)
    if not n:
        return None
    if n in _INDEX:
        return CATALOG[_INDEX[n]]
    # Substring match — prefer the longest indexed token contained in the query
    # (or vice-versa) so we don't match "s30" inside an unrelated string.
    best_key, best_len = None, 0
    for token, key in _INDEX.items():
        if len(token) < 3:
            continue
        if (token in n or n in token) and len(token) > best_len:
            best_key, best_len = key, len(token)
    return CATALOG[best_key] if best_key else None


def catalog_list() -> list[dict]:
    """The full catalog as a list of {key, ...spec, ...derived} dicts for the app."""
    out = []
    for key, spec in CATALOG.items():
        d = spec.to_dict()
        d["key"] = key
        d.update(derive_params(spec))
        out.append(d)
    return out


# ── Derivation: physical specs → algorithm parameters ───────────────────────────

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def derive_params(spec: TelescopeSpec) -> dict:
    """
    Compute the photometry/scheduler parameters from physical specs.

    The pipeline (``photometry.run_pipeline``) and the scheduler read these,
    not the raw hardware numbers.  Each formula is anchored on the Seestar S50
    so its values are unchanged; other scopes scale by the physics:

      pixel_scale_arcsec = 206.265 · pixel_size_µm / focal_length_mm
      fov_deg            = pixel_scale · sensor_width_px / 3600
      focal_ratio        = focal_length / aperture
      mag_limit (faint)  = 15.0 + 5·log10(aperture/50) + (0.5 if cooled)
                           — faint reach grows with light grasp (∝ aperture²)
      mag_min  (sat. cut)= 10.0 + 2.5·log10((aperture/50)² · (max_exp/30))
                           — more light / longer subs saturate fainter stars,
                             so the bright comp-star cutoff moves faint-ward
      field_radius_deg   = max(0.25, 0.4 · fov_deg) — comp search ~⅖ of the FOV
      fwhm_fallback_px   = clamp(9.6 / pixel_scale, 2.0, 6.0) — last-resort PSF
                           width when DAOStarFinder finds nothing (S50 → 4.0)

    Returns a flat dict ready to merge into ``config['photometry']`` and to
    populate ``NodeInfo`` registration fields.
    """
    ap = float(spec.aperture_mm or 0.0)
    fl = float(spec.focal_length_mm or 0.0)
    px = float(spec.pixel_size_um or 0.0)

    out: dict[str, Any] = {
        "telescope_model": spec.display_name,
        "tier": int(spec.tier),
        "aperture_mm": round(ap, 2),
        "focal_length_mm": round(fl, 2),
        "mount_type": spec.mount_type,
        "max_exposure_s": float(spec.max_exposure_s),
        "camera_model": spec.camera_model or spec.sensor_name,
        "cooled_camera": bool(spec.cooled),
        "gain": float(spec.gain_e_per_adu),
        "read_noise": float(spec.read_noise_e),
        "filter_set": list(spec.default_filters),
    }

    # Pixel scale + FOV need optics + sensor; skip gracefully if unknown (custom).
    if fl > 0 and px > 0:
        pixel_scale = ARCSEC_PER_RADIAN * (px / 1000.0) / fl  # = 206.265·µm/mm
        out["pixel_scale_arcsec"] = round(pixel_scale, 3)
        if spec.sensor_w_px:
            out["fov_deg"] = round(pixel_scale * spec.sensor_w_px / 3600.0, 3)
        out["fwhm_fallback_px"] = round(_clamp(9.6 / pixel_scale, 2.0, 6.0), 2)
        if "fov_deg" in out:
            out["field_radius_deg"] = round(max(0.25, 0.4 * out["fov_deg"]), 2)
    if ap > 0:
        out["focal_ratio"] = round(fl / ap, 2) if ap else 0.0
        light_grasp = (ap / 50.0) ** 2
        cooled_bonus = 0.5 if spec.cooled else 0.0
        out["mag_faint_limit"] = round(15.5 + 2.5 * math.log10(light_grasp) + cooled_bonus, 1)
        out["mag_limit"] = round(15.0 + 2.5 * math.log10(light_grasp) + cooled_bonus, 1)
        out["mag_bright_limit"] = round(6.0 + 2.5 * math.log10(light_grasp), 1)
        exp_factor = max(1e-3, (spec.max_exposure_s / 30.0))
        out["mag_min"] = round(10.0 + 2.5 * math.log10(light_grasp * exp_factor), 1)

    return out


# ── Config enrichment (mirrors geolocation.enrich_config_with_location) ──────────

# Maps derived-param keys → the config["photometry"] keys the pipeline reads.
_PHOT_KEYS = {
    "pixel_scale_arcsec": "pixel_scale",
    "field_radius_deg": "field_radius",
    "mag_limit": "mag_limit",
    "mag_min": "mag_min",
    "read_noise": "read_noise",
    "gain": "gain",
    "fwhm_fallback_px": "fwhm_fallback_px",
    "max_exposure_s": "max_exposure_s",
}


def enrich_config_with_telescope(config: dict) -> dict:
    """
    Resolve ``observatory.telescope`` to a catalog spec and write the derived
    photometry parameters into ``config['photometry']`` — unless the operator
    has set them explicitly, in which case the explicit value always wins.

    Mirrors ``geolocation.enrich_config_with_location``: safe to call on every
    boot, idempotent, and a no-op when the model is unknown or already tuned.
    """
    if config is None:
        config = {}

    obs = config.get("observatory", {})
    model = obs.get("telescope") or obs.get("telescope_model") or ""
    spec = lookup(model)
    if spec is None:
        logger.debug("Telescope model %r not in catalog — leaving photometry config as-is", model)
        return config

    derived = derive_params(spec)
    phot = config.setdefault("photometry", {})
    applied = []
    for src, dst in _PHOT_KEYS.items():
        if src not in derived:
            continue
        # Explicit config value wins; only fill where unset.
        if phot.get(dst) in (None, "", 0, 0.0):
            phot[dst] = derived[src]
            applied.append(f"{dst}={derived[src]}")

    # Record the resolved display name + camera so registration/UI show the truth.
    obs.setdefault("instrument", derived.get("camera_model", ""))
    config["observatory"] = obs

    if applied:
        logger.info("Telescope %s → photometry params: %s", spec.display_name, ", ".join(applied))
    return config


# ── ALPACA autodetection (the "any ALPACA equipment" path) ──────────────────────

def detect_telescope_specs(telescope=None, camera=None,
                           fallback_model: str = "") -> dict:
    """
    Read real hardware specs off a connected ALPACA telescope + camera.

    Camera fields (sensor name, pixel size, sensor dimensions, gain, cooler)
    come from the wrappers in ``alpaca/camera.py``.  Aperture and focal length
    come from the standard ALPACA Telescope properties ``aperturediameter`` and
    ``focallength`` (metres) — many smartscope bridges omit these, so each read
    is guarded.  Anything the device can't report is back-filled from the
    catalog entry for ``fallback_model``.

    Returns a ``derive_params``-style dict (so callers can merge it straight
    into config / the registration payload).  Returns {} if nothing readable.
    """
    spec = lookup(fallback_model) or TelescopeSpec()
    spec = TelescopeSpec.from_dict(spec.to_dict())  # copy so we don't mutate CATALOG
    got_anything = False

    # Telescope optics (metres in ALPACA → mm)
    if telescope is not None:
        conn = getattr(telescope, "_c", None)
        for prop, attr, scale in (("aperturediameter", "aperture_mm", 1000.0),
                                   ("focallength", "focal_length_mm", 1000.0)):
            try:
                val = float(conn._get(prop)) * scale if conn is not None else None
                if val and val > 0:
                    setattr(spec, attr, round(val, 2))
                    got_anything = True
            except Exception as exc:
                logger.debug("ALPACA telescope %s unavailable: %s", prop, exc)

    # Camera sensor
    if camera is not None:
        for getter, attr, cast in (
            ("sensor_name", "sensor_name", str),
            ("pixel_size_x", "pixel_size_um", float),
        ):
            try:
                fn = getattr(camera, getter, None)
                val = fn() if callable(fn) else None
                if val:
                    setattr(spec, attr, cast(val))
                    got_anything = True
            except Exception as exc:
                logger.debug("ALPACA camera %s unavailable: %s", getter, exc)
        ccam = getattr(camera, "_c", None)
        if ccam is not None:
            try:
                spec.sensor_w_px = int(ccam._get("cameraxsize"))
                spec.sensor_h_px = int(ccam._get("cameraysize"))
                got_anything = True
            except Exception as exc:
                logger.debug("ALPACA camera sensor size unavailable: %s", exc)
        try:
            spec.cooled = bool(camera.cooler_on())
        except Exception:
            pass
        try:
            spec.camera_model = spec.sensor_name or spec.camera_model
        except Exception:
            pass

    if not got_anything:
        return {}

    derived = derive_params(spec)
    logger.info("ALPACA autodetect: %s → %s", spec.sensor_name or fallback_model, derived)
    return derived
