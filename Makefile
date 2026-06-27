API_BASE   := https://api.thetelescope.net
BASE_HREF  := /

# Build the Flutter PWA and commit the output so Railway picks it up
.PHONY: build-web
build-web:
	cd app && flutter build web --release \
		--base-href=$(BASE_HREF) \
		--dart-define=API_BASE=$(API_BASE)

# Build + stage + commit + push in one shot
.PHONY: deploy-web
deploy-web: build-web
	git add app/build/web/
	git commit -m "chore: rebuild Flutter web for production"
	git push origin main

# Deploy the cloud server to Railway
.PHONY: deploy-cloud
deploy-cloud:
	railway up --detach

# Deploy everything
.PHONY: deploy
deploy: deploy-web deploy-cloud
