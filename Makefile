API_BASE   := https://api.thetelescope.net
BASE_HREF  := /

# Build the Flutter PWA locally for verification.
.PHONY: build-web
build-web:
	cd app && flutter build web --release \
		--base-href=$(BASE_HREF) \
		--dart-define=API_BASE=$(API_BASE)

# Railway now builds the Flutter PWA from source via Dockerfile.app.
.PHONY: deploy-web
deploy-web:
	git push origin main

# Deploy the cloud server to Railway
.PHONY: deploy-cloud
deploy-cloud:
	railway up --detach

# Deploy everything
.PHONY: deploy
deploy: deploy-web deploy-cloud

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
