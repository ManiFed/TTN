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

# Run the full test suite (unit tests + reliability gauntlet)
.PHONY: test
test:
	venv/bin/python -m unittest discover -s tests -t .

# Run only the autonomous-node reliability gauntlet (fault-injection tests)
.PHONY: gauntlet
gauntlet:
	venv/bin/python -m unittest discover -s tests/gauntlet -t .
