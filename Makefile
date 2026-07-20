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

# ── Fuzz / simulation hardening campaign (tests/fuzz/, sim/) ─────────────────
# fuzz-smoke is deterministic and CI-safe; the others are long manual runs.
.PHONY: fuzz-smoke fuzz-node fuzz-cloud fuzz-triage
fuzz-smoke:
	$(PYTHON) -m pytest tests/fuzz/test_smoke.py -q

# Node agent vs. fake ALPACA hardware with fault injection (subprocess/seed).
# SEEDS=0:5000 PROFILE=heavy make fuzz-node
fuzz-node:
	$(PYTHON) -m tests.fuzz.runner --seeds $(or $(SEEDS),0:500) \
		--profile $(or $(PROFILE),mixed) --parallel $(or $(PARALLEL),8)

# Cloud API vs. mutated payloads on an ephemeral local PostgreSQL.
fuzz-cloud:
	$(PYTHON) -m tests.fuzz.fuzz_cloud --requests $(or $(REQUESTS),50000) \
		--seed $(or $(SEED),1)

# Group a run's failures by signature: make fuzz-triage RUN=sim_results/fuzz_node/<id>
fuzz-triage:
	$(PYTHON) -m tests.fuzz.triage $(RUN)

# One-command photometry validation: regression suite + synthetic corpus gates.
# Exits non-zero if any accuracy/quality gate fails.
.PHONY: validate
validate:
	$(PYTHON) -m pytest tests/validation tests/test_photometry_features.py -q
	$(PYTHON) scripts/validate_photometry.py --synthetic --out cloud_data/validation

# Read-only readiness audit for a beta node (config/dependency/disk presence only).
.PHONY: preflight
preflight:
	$(PYTHON) scripts/node_preflight.py

# Same, but actually exercises cloud auth, directory writes, and the solver
# launch instead of just checking they look configured. Run this before each
# observing session — it's slower and touches the network, but it's the
# check that catches real nightly breakage before it happens live.
.PHONY: preflight-active
preflight-active:
	$(PYTHON) scripts/node_preflight.py --active

# Create/check the real-data corpus described in docs/validation/FIXTURE_MANIFEST.md.
.PHONY: beta-init beta-audit
beta-init:
	$(PYTHON) scripts/beta_capture.py init
beta-audit:
	$(PYTHON) scripts/beta_capture.py audit
