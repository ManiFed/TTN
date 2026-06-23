# The Telescope Net — Complete Vision & Website Plan

> This document is the canonical record of the The Telescope Net vision, technical
> architecture, science program, organization, and roadmap. It also serves as the
> full specification for the public website. Building the website and building the
> network are the same act: every page described below corresponds to something
> that must exist operationally. The PDFs this supersedes are
> `The Telescope Net.pdf`, `BS_TECHSTACK_V2.pdf`, and `node_guidelines.pdf`.

---

## The Mission

The Telescope Net is an accessible astronomy charity that operates the world's first
planet-wide hunt for the stars that professional observatories miss.
Seestar smart telescope owners donate their telescope's nights to the network. Our
pipeline automatically schedules observations of scientifically valuable targets,
processes the data into calibrated photometry, and submits it to professional
astronomical databases. Members contribute to real science — supernovae, novae,
variable stars, transient phenomena — without physical effort, technical expertise,
or significant cost.

The night sky belongs to everyone. The Telescope Net makes that true.

---

## The Problem

Traditional amateur astronomy requires carrying heavy equipment to dark locations,
standing in the cold for hours, operating fine controls with precision, looking through
an eyepiece with functional vision, and traveling to sites inaccessible to wheelchairs.
Every one of those requirements excludes someone.

Existing citizen science programs offer classifications of existing images — clicking on
galaxies, identifying craters. This is not astronomy. Contributors are not named on
scientific outputs. They do not collect original data. They are not astronomers.

Professional telescope networks like Las Cumbres Observatory are inaccessible to the
public. Consumer smart telescope networks like Unistellar are closed platforms with
proprietary hardware costing thousands of dollars and no accessibility focus.

Nobody has solved this. Not because they tried and failed. Because nobody tried.

---

## The Solution

The Telescope Net is a three-part system:

1. A distributed network of donated Seestar telescopes operating autonomously overnight
2. An AI-driven pipeline that decides what to observe, processes the data, and submits
   it to science
3. A fully accessible mobile app that gives anyone — regardless of physical ability —
   a genuine connection to the science their telescope produces

Members contribute their telescope's nights to the network. The network does the science.
The science is real, credited, and permanent.

---

## Website Structure

The website is both the public face of the organization and the operational backbone
members interact with. It has four distinct audiences: prospective members, active nodes,
scientists/partners, and donors/grant reviewers. Every section below is both a web page
and a statement of what must exist in the product.

---

### Page: Home

Establishes the mission in one sentence and directs each visitor type to their path.

- Hero: "Your telescope. Real science. Anyone." with a live counter of active nodes and
  recent AAVSO submissions from the network.
- Three clear entry points: Join the Network / See the Science / Support the Mission.
- Accessibility statement prominently placed (not buried in a footer).

---

### Page: How It Works

Explains the member experience without technical jargon.

**Joining (one-time, ~15 minutes):**
1. Download the The Telescope Net Node Agent (free, one-click installer for Windows/Mac/Linux)
2. Create an account
3. Enable Station Mode on your Seestar (connects it to your home WiFi)
4. Run the Node Agent — it finds your Seestar and registers automatically
5. Your node is live

**Every night (the member does nothing):**
- At astronomical twilight the Node Agent wakes up and downloads tonight's observation plan
- The Seestar slews to each target, takes stacked exposures, and saves images to the local
  network share
- The Node Agent runs photometry on each image and uploads measurements (~1 KB each) to
  the The Telescope Net cloud
- At dawn the Seestar parks and a night summary is generated

**Every morning:**
- Member receives a notification: which targets were observed, whether the data was accepted,
  and confirmation of any AAVSO submission. Their name is on the record.

---

### Page: The Science

Explains the scientific value proposition for members and for professional partners.

**Primary program — Bright Transient Follow-up:**

Targets: classical novae (mag 4–12 at peak), supernovae in nearby galaxies (mag 10–16),
cataclysmic variable outbursts (mag 10–15), AGN flares (mag 12–15).

Value: dense light curve sampling, 24-hour continuous coverage via geographic distribution
of nodes, rapid response (< 1 hour from alert), data complementary to professional
facilities — different time windows, continuous monitoring they cannot provide.

**Secondary program — Variable Star Monitoring:**

Targets: long-period variables (Miras, semi-regulars), eclipsing binaries (timing program),
cataclysmic variables (quiescent monitoring), Be stars (outburst monitoring), active
galactic nuclei (long-term baseline).

**Steady-state filling:**

When no high-priority targets are available, nodes observe a curated AAVSO monitoring list
to remain productive every clear night.

**Data pipeline summary (technical detail for science partners):**

| Layer | Function | Implementation |
|-------|----------|---------------|
| 1 | Alert ingestion | ALeRCE, ATLAS, ASAS-SN, AAVSO, Gaia, TNS polled hourly |
| 2 | Scoring | Composite score: brightness × science × time-criticality × coverage-gap × observability |
| 3 | Scheduling | Nightly greedy dispatch with real-time interrupt handling |
| 4 | Node control | ALPACA API via seestar_alp; SMB image retrieval |
| 5 | Local photometry | ASTAP plate solve → comparison stars → aperture photometry → differential photometry |
| 6 | Cloud validation | Cross-node agreement, SNR/uncertainty/airmass quality gates, light curve management |
| 7 | Submission | AAVSO WebObs API (daily batch); TNS reporting for significant novel detections |

**Scoring function detail:**

```
Score = brightness_match × scientific_value × time_criticality × coverage_gap × observability

Scientific value by type:
  Classical nova:          1.0
  Type Ia supernova:       0.95
  CV outburst:             0.9
  Core-collapse supernova: 0.85
  AGN flare:               0.7
  Mira at maximum:         0.6
  Eclipsing binary:        0.5

Time criticality decay:
  Kilonova:        hours
  Nova:            days
  Supernova:       days–weeks
  CV outburst:     days
  Mira:            weeks
  Eclipsing binary: fixed schedule

Observability requirements:
  Altitude > 30° from node location
  Moon distance > 30°
  Weather probability factored per node
```

**Quality control gates (before AAVSO submission):**
- SNR > 20
- Uncertainty < 0.3 mag
- At least 3 comparison stars
- Airmass < 3.0
- Borderline measurements go to human review queue

**AAVSO submission format:**
```
#TYPE=EXTENDED
#OBSCODE=[network observer code]
#SOFTWARE=The Telescope Net Pipeline v[X]
#DELIM=,
#DATE=BJD
#OBSTYPE=CCD
Fields: STARID, DATE, MAGNITUDE, MAGERR, FILTER, TRANSFORMED, MTYPE,
        CNAME, CMAG, KNAME, KMAG, AMASS, GROUP, CHART, NOTES
```

---

### Page: The App

The mobile app (Flutter, iOS + Android + PWA) is the primary member experience.
It is designed from the ground up to run unattended overnight — no expertise, no dark-field trips, just
scratch around the question: how do disabled people experience astronomy and what do
they need?

**Design principles:**
1. **Autonomous-first** — every decision starts with whether the network can run without user intervention
2. **Multiple modalities** — every piece of information available as visual, audio,
   haptic, and text
3. **No required precision** — every interaction achievable with one finger, voice
   command, single switch, or eye gaze
4. **Patience** — no time limits, no auto-advancing content, user controls the pace
5. **Plain language** — jargon defined when used, reading level adjustable, audio
   available for all text

**Core screens:**

*Home* — telescope status (online/offline/observing), current target, last night summary,
network-wide activity (how many telescopes observing now, across how many countries)

*Results* — per-target light curves with three representation modes: chart, data table,
and audio description. Haptic light curve pattern on supported devices.

*Impact* — cumulative stats (total observations, AAVSO acceptance rate, clear nights),
links to ATels and papers that used the member's data, achievements system

**Achievements (gamification layer):**
- First Light (first observation)
- Century Club (100 observations)
- Nova Hunter (observed a nova)
- Night Owl (500 observations)
- Supernova Hunter (observed 3 supernovae)

**Accessibility feature checklist:**

*Visual:* Full VoiceOver (iOS) and TalkBack (Android) support; dynamic text sizing;
high contrast mode; reduced motion mode; color-blind safe palette; alt text on all
images; text/table alternatives for all charts; dark mode; OpenDyslexic font option.

*Motor:* Single-tap navigation; switch control; voice control with unique labels on all
interactive elements; eye gaze compatible (44×44pt minimum touch targets); no time-limited
interactions; no required motion gestures.

*Cognitive:* Consistent navigation structure; plain language throughout; reading level
selector (child / standard / expert); jargon definitions on tap; no unexpected
interruptions; reduce visual complexity mode; no auto-playing content.

*Hearing:* No audio-only information; all sounds have visual equivalents; captions on
all video; vibration alternatives for audio alerts.

*Novel features:*
- Audio descriptions of all data (three depth levels)
- Sonification of light curves
- Haptic light curves
- Spatial audio sky map (hold phone up, hear what's overhead)
- Patience mode (no rushing, ever)
- Proxy observation support (caregiver setup, member receives results)

**Tech stack:** Flutter/Dart; fl_chart (light curves); flutter_tts (audio descriptions);
vibration package (haptic patterns); sensors_plus (accelerometer for sky map);
firebase_messaging (push notifications); http (cloud API client).

---

### Page: Join / Node Builder

This is the primary conversion page. It has two paths, and the primary path is always
presented first.

---

#### Primary Path: Bring Your Own Equipment (Free)

If you have a Seestar, you can join right now at no cost.

```
Requirements:
  ✓ Seestar S50 or S30 (or any ALPACA-compatible scope, subject to approval)
  ✓ A computer (Windows, Mac, or Linux) that can be left on overnight
  ✓ Home WiFi network
  ✓ Power outlet
  ✓ Ability to follow a setup guide — that's it

The Telescope Net provides everything else:
  → Node software (free download, one-click installer)
  → Scheduling and coordination
  → All data processing
  → All scientific submissions
  → Full app and dashboard access
  → Your name on every observation
```

CTA: **Download Node Agent** → triggers installer download + account creation flow.

Every free node gets a Node Activation Code (e.g., BS-2026-XYZ) that auto-registers
on first boot and ties the node to the member's account. Delivered digitally at signup;
optionally printed on a physical card for members who prefer it (accessibility option).

---

#### Secondary Path: Node Builder (Guided Hardware Configurator)

It's a storefront if you don't have the necessary equipment to operate a node.

The Node Builder is a sleek, dark space-themed mobile-first wizard:
- Starts with a Hardware Inventory Quiz to detect what the user already has
- Persistent summary panel showing estimated science impact and uptime for the build
- Hero preset: "Complete Autonomous Node" — fully assembled recommendation

**Step 1 — Hardware Quiz:** What do you already own? Personalises all subsequent
recommendations.

**Step 2 — Telescope:**
- Recommended: Seestar S50 or S30 Pro
- Tiered options with science impact scores shown alongside each
- "I already own one" always shown first and selected by default

**Step 3 — Computer:**
- "I already have one" shown first and selected by default
- Raspberry Pi 5 (recommended standalone option — low power, leaves overnight)
- Raspberry Pi 4B (budget)
- Mac Mini (premium)

**Step 4 — Addons (modular, all optional, collapsible sections):**

*Power & Autonomy:*
- Smart power box (remote power cycling via Node Agent)
- SwitchBot (automated physical controls)
- UPS / battery backup
- Solar power kit (for remote locations)

*Protection:*
- Minidome enclosure + weather sensors (enables fully outdoor, unattended operation)
- Dew heater (climate-dependent)

*Connectivity:*
- WiFi extender / mesh node (for scopes placed away from router)

**Step 5 — Review:** Shows the complete build with links to purchase each component
at standard retail price. No checkout, no markup, no The Telescope Net transaction.

**Complete Autonomous Node preset** (the flagship recommendation):
- Seestar S50
- Raspberry Pi 5 preloaded with Node Agent
- Smart Power Box + SwitchBot + UPS
- Minidome + weather sensors
- WiFi extender

Approximate cost for the full preset: $1,600–$2,200 at retail. Members can remove
any item to reduce cost and complexity.

**Node Agent on Pi:** Nodes built around a Raspberry Pi get the same one-click Node
Agent as any other node. The Pi-specific version adds an optional MQTT bridge that
allows the Node Agent to control smart power addons (power cycling the Seestar if it
hangs, reading weather sensor data for the local conditions feed). This is an optional
layer — the Node Agent works identically without it.

**Branding for purchased/assembled nodes (optional):**
- Matte black vinyl wrap with The Telescope Net logo + QR plate
- Custom boot splash and Node Agent branding on Pi builds
- Premium constellation-themed packaging for gifted/loaned units
- Activation card (large print + audio QR) included in all shipped kits

---

### Page: Network Status (Public Dashboard)

Live read-only view of the network, publicly accessible without login:
- Number of active nodes by region (map)
- Targets being observed tonight
- Recent measurements submitted (anonymised until member opts into public credit)
- Cumulative AAVSO submission count
- Current alert queue (targets being tracked)

This page demonstrates the network is real and producing science. It is the primary
proof point for grant applications and partnership discussions.

---

### Page: Science Partners

For AAVSO, professional astronomers, and institutions.

- How to request priority observations from the network
- Data access API (public read, no auth required for historical photometry)
- Data quality documentation (pipeline details, precision characterisation)
- Contact for coordination on specific targets

---

### Page: About & Partners

The organization, founding story, and partner logos.

**Founded:** 2025 by Eli Goldfine and Scott Mellis.

**Priority partnerships:**

| Partner | Ask | Offer |
|---------|-----|-------|
| ZWO | Official API docs, telescope donations for loan program, co-marketing | Marketing story ("Seestar does real science"), community of engaged users |
| AAVSO | Multi-node observer code, programmatic submission API, possible fiscal sponsorship | Significant submission volume increase, new demographics, accessible astronomy alignment |
| American Foundation for the Blind | User testing pool, community access | Genuine astronomy access, novel accessible technology |
| National Federation of the Blind | Same | Same |
| Christopher & Dana Reeve Foundation | Co-grant applications, endorsement | Named participation in real science |
| Perkins School for the Blind | Node hosting, student participants | Free network access, curriculum materials, student names on scientific outputs |

---

### Page: Donate / Support

Grant-oriented funding model with individual donation option.

**Primary funding targets:**
- NSF (education and public outreach programs)
- NASA citizen science program grants
- Simons Foundation (astronomy + education)
- Chan Zuckerberg Initiative (science)
- Google.org
- Apple accessibility grants
- American Foundation for the Blind
- Christopher & Dana Reeve Foundation
- Astronomical Society of the Pacific

**In-kind donation targets:**
- AWS / Azure / Google Cloud nonprofit credits ($5,000–$25,000/year free compute)
- ZWO telescope donations for loan program
- Equipment from retiring amateur astronomers

**Individual giving options:**
- Monthly donors
- One-time donations
- Memorial / tribute donations ("A telescope observed in memory of…")
- Node naming rights for major donors

**Explicitly not a revenue model:** member subscription fees, hardware sales, advertising.

---

## Technical Architecture

### Cloud Infrastructure

| Component | Phase 0–1 (current) | Production target |
|-----------|---------------------|-------------------|
| Application server | Python / Flask | Python / FastAPI |
| Database | SQLite | PostgreSQL (managed) |
| Async task queue | Python threads + cron | Celery + Redis |
| Image storage | Local filesystem | Cloudflare R2 (no egress fees) |
| Hosting | Single VPS (DigitalOcean / AWS) | Same, AWS preferred once nonprofit credits obtained |

**Database schema (current):** `nodes`, `targets`, `scores`, `plans`, `measurements`,
`aavso_batches`, `interrupts`. Missing tables to add: `users`, `members`, `notifications`,
`review_queue`.

**Cloud API surface:**

*Node-authenticated endpoints:*
- `POST /api/v1/nodes/register` — first-boot registration with activation code
- `POST /api/v1/nodes/heartbeat` — 60-second keepalive + conditions report
- `GET  /api/v1/plan` — download tonight's observation plan
- `POST /api/v1/measurements` — submit photometry result
- `POST /api/v1/images` — upload FITS (stored 30 days, then pruned)
- `GET  /api/v1/interrupts` — poll for high-priority target interrupts

*Public read endpoints (no auth):*
- `GET /api/v1/targets` — active target list
- `GET /api/v1/lightcurves/<target>` — historical photometry for a target
- `GET /api/v1/network/status` — live node and submission summary

*Member-authenticated endpoints (to be built):*
- Account creation, login, node association
- Per-member observation history and statistics
- Night summary retrieval
- Notification preferences

*Admin endpoints:*
- `POST /api/v1/admin/ingest` — trigger manual alert ingestion
- `POST /api/v1/admin/replan` — trigger manual scheduling run

### Node Software

**Language:** Python 3.10+  
**Entry point:** `main.py` (watchdog) → `dashboard.py` (operator UI + control loop)

**Key modules:**

| Module | Function |
|--------|----------|
| `alpaca/device_manager.py` | Connect / disconnect all devices |
| `alpaca/telescope.py` | Slew, track, park, RA/Dec query |
| `alpaca/camera.py` | Expose, check status, FITS export |
| `alpaca/focuser.py` | Move, halt, position query |
| `alpaca/autofocus.py` | V-curve sweep → parabolic minimum |
| `alpaca/filterwheel.py` | Set position, query filter names |
| `alpaca/platesolve.py` | ASTAP solve → WCS → closed-loop centering |
| `alpaca/safety_manager.py` | Altitude limits, auto-park conditions |
| `photometry.py` | Full aperture photometry pipeline |
| `stacking.py` | Live RANSAC-aligned sub-pixel stacking |
| `cloud_communicator.py` | Registration, heartbeat, plan download, measurement upload |
| `aavso_submission.py` | AAVSO Extended Format + WebObs API |
| `image_watcher.py` | FSEvents/inotify watcher on SMB share directory |
| `geolocation.py` | Auto-detect node location for scheduling |
| `fits_export.py` | FITS file management |

**Safety behaviour:**
- Heartbeat to cloud every 60 seconds
- Auto-park if cloud connection lost > 30 minutes
- Auto-park at astronomical dawn regardless
- Graceful handling of Seestar disconnects
- Local log of all actions

**Packaging (to be built):**
- PyInstaller → single executable
- NSIS → Windows installer (auto-configures sleep/idle prevention)
- DMG creator → macOS installer (installs launchd plist for service mode)
- AppImage → Linux (installs systemd service)
- Installer configures OS sleep prevention automatically (critical for overnight operation)

### Mobile App

**Framework:** Flutter / Dart  
**Platforms:** iOS, Android, PWA  

**Key packages:** `fl_chart`, `flutter_tts`, `vibration`, `sensors_plus`,
`firebase_messaging`, `shared_preferences`, `http`

**Status:** Not yet started. Phase 1 milestone.

---

## Node Tiers

| Tier | Equipment | Filter | Limiting mag | Precision | Status |
|------|-----------|--------|-------------|-----------|--------|
| 1 — Standard | Seestar (any model) | Broadband CV/CR | ~14 | 0.05–0.1 mag | Active (primary network) |
| 2 — Filtered | 6–8" scope + mono camera + BVRI | Johnson-Cousins BVRI | ~17–18 | 0.01–0.02 mag | Future |
| 3 — Spectroscopy | Tier 2 + spectrograph | — | — | Classification | Future |

Tier 1 is the entire network for Phase 0–2. Anyone with a Seestar can contribute.

**NINA / EKOS / ASIAIR support:** Phase 4 future. The ALPACA protocol abstraction layer
in `alpaca/` is designed to make driver swaps straightforward.

---

## Geographic Strategy

24-hour continuous coverage requires nodes distributed across longitude zones:

| Zone | Region | UTC offset | Target share | Coverage window (UTC) |
|------|--------|-----------|-------------|----------------------|
| 1 | Americas | −8 to −4 | 30% | 01:00–09:00 |
| 2 | Europe / Africa | 0 to +3 | 30% | 20:00–04:00 |
| 3 | Asia / Pacific | +8 to +12 | 30% | 11:00–19:00 |
| 4 | Middle East / India | +4 to +7 | 10% | Gap-filling |

With this distribution any target can be observed continuously for 24 hours by different
nodes. No target is ever out of reach.

---

## Development Roadmap

### Phase 0: Proof of Concept (Months 1–3) — Budget ~$100

Goal: Prove the science works.

- [x] seestar_alp controlling a Seestar via ALPACA API
- [x] Images retrieved from Seestar via SMB share automatically
- [x] Photometry pipeline producing calibrated magnitudes from Seestar images
- [x] Cloud server with alert ingestion, scoring, scheduling
- [x] Node–cloud communication (registration, heartbeat, plan, measurement upload)
- [ ] First AAVSO-accepted observation from automated pipeline

Success criterion: one AAVSO-accepted automated observation from a Seestar with magnitude
agreeing within 0.15 mag of known value.

### Phase 1: Core System (Months 3–9) — Budget ~$500–$1,000

Goal: Build the real multi-node system.

- [ ] Node software packaged as one-click installer with sleep prevention
- [ ] Service management (Windows Service / launchd / systemd)
- [ ] Member account system (user table, login, node association)
- [ ] Night summary generation and push notification dispatch
- [ ] First Flutter app (basic dashboard, accessibility-first)
- [ ] 3–5 beta nodes running
- [ ] First multi-node coordinated observation
- [ ] Public read API endpoints live
- [ ] Node Builder web configurator (BYOD path live, hardware guide secondary)
- [ ] Nonprofit filing submitted

### Phase 2: Launch (Months 9–18) — Budget: first grant ($5,000–$25,000)

Goal: Public launch, grow to 50 nodes.

- [ ] App on iOS and Android App Stores
- [ ] 25 → 50 active nodes
- [ ] Nonprofit status obtained
- [ ] First grant received
- [ ] First ATel with The Telescope Net data
- [ ] ZWO partnership established
- [ ] AAVSO formal partnership and multi-node observer code
- [ ] Human review queue for borderline measurements
- [ ] TNS outbound reporting for significant novel detections
- [ ] First network member testimony documented

### Phase 3: Growth (Months 18–36) — Budget: $50,000–$200,000/year

Goal: 200 nodes, recognised scientific instrument.

- [ ] 100 → 200 active nodes, 25+ countries
- [ ] 10,000+ AAVSO submissions
- [ ] Network paper submitted
- [ ] Disabled astronomer's data cited in peer-reviewed paper
- [ ] Telescope loan program active (10+ scopes loaned)
- [ ] LIGO / gravitational wave follow-up participation
- [ ] Tier 2 filtered nodes introduced
- [ ] Part-time staff hired
- [ ] $50,000+ annual grant funding secured

---

## Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|-----------|
| seestar_alp breaks on ZWO firmware update | Medium | Temporary node outages | Monitor releases; version-lock stable firmware; contribute to seestar_alp |
| Seestar photometric precision insufficient | Low | Science value reduced | Test in Phase 0 before building anything else; pivot to detection confirmation if needed |
| Member computers sleeping during night | High (if not configured) | Node offline | Installer configures sleep prevention automatically |
| SMB share access issues | Low–Medium | Images not retrieved | Test during setup wizard; provide troubleshooting guide; HTTP fallback |
| ZWO changes Seestar protocol | Low–Medium | High (whole network) | Pursue official API partnership; ALPACA abstraction layer eases driver swap |
| Insufficient nodes in key longitudes | Medium | Coverage gaps | Active recruitment; international astronomy club partnerships; telescope loan program |

---

## Success Metrics

| Phase | Milestone |
|-------|-----------|
| Phase 0 (month 3) | One AAVSO-accepted automated observation; photometric precision validated |
| Phase 1 (month 9) | 5 active nodes in 3+ countries; 100+ AAVSO submissions; first app version; nonprofit filing submitted |
| Phase 2 (month 18) | 50 nodes; 1,000+ submissions; app on App Stores; first ATel; nonprofit status; first grant |
| Phase 3 (month 36) | 200 nodes; 25+ countries; 10,000+ submissions; network paper published; network member's data cited in published research |

---

*The Telescope Net | telescopenet.org*  
*Founded 2025 by Eli Goldfine and Scott Mellis*  
*"The night sky belongs to everyone."*
