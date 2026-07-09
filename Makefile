# Deploy the cloud server to Railway
.PHONY: deploy-cloud
deploy-cloud:
	railway up --detach

# Deploy everything
.PHONY: deploy
deploy: deploy-cloud

# ── Testing & scientific validation ───────────────────────────────────────────
# Offline, deterministic, network-free.  See docs/validation/VALIDATION_REPORT.md
PYTHON ?= python3

# Run the full test suite (unit tests + reliability gauntlet)
.PHONY: test
test:
	$(PYTHON) -m pytest tests -q

# Run only the autonomous-node reliability gauntlet (fault-injection tests)
.PHONY: gauntlet
gauntlet:
	$(PYTHON) -m pytest tests/gauntlet -q

# One-command photometry validation: regression suite + synthetic corpus gates.
# Exits non-zero if any accuracy/quality gate fails.
.PHONY: validate
validate:
	$(PYTHON) -m pytest tests/validation tests/test_photometry_features.py -q
	$(PYTHON) scripts/validate_photometry.py --synthetic --out cloud_data/validation

# Read-only readiness audit for a beta node.
.PHONY: preflight
preflight:
	$(PYTHON) scripts/node_preflight.py

# Create/check the real-data corpus described in docs/validation/FIXTURE_MANIFEST.md.
.PHONY: beta-init beta-audit
beta-init:
	$(PYTHON) scripts/beta_capture.py init
beta-audit:
	$(PYTHON) scripts/beta_capture.py audit
