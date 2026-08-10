IMAGE ?= cursor-openai-proxy
TAG ?= 0.1.3
CHART := chart
RELEASE ?= cursor-bridge
NAMESPACE ?= cursor-bridge

# GitHub Container Registry
#   export GITHUB_USER=your-github-username
#   echo $GITHUB_TOKEN | docker login ghcr.io -u $GITHUB_USER --password-stdin
GHCR_OWNER ?= $(GITHUB_USER)
GHCR_IMAGE ?= ghcr.io/$(GHCR_OWNER)/$(IMAGE)
PLATFORMS ?= linux/amd64,linux/arm64

.PHONY: docker-build docker-run helm-lint helm-template helm-install helm-upgrade helm-uninstall kind-load \
	ghcr-login ghcr-push ghcr-push-multi

docker-build:
	docker build -t $(IMAGE):$(TAG) .

# Requires CURSOR_API_KEY in the environment.
docker-run:
	docker run --rm -p 8765:8765 \
	  -e CURSOR_API_KEY="$${CURSOR_API_KEY}" \
	  -e HOST=0.0.0.0 \
	  $(IMAGE):$(TAG)

kind-load: docker-build
	kind load docker-image $(IMAGE):$(TAG)

# --- GHCR ---
# Create a classic PAT or fine-grained token with write:packages (and read:packages).
# For org images, also grant package write on the org.
ghcr-login:
	@test -n "$${GITHUB_USER}" || (echo "Set GITHUB_USER" && exit 1)
	@test -n "$${GITHUB_TOKEN}" || (echo "Set GITHUB_TOKEN (PAT with write:packages)" && exit 1)
	echo "$${GITHUB_TOKEN}" | docker login ghcr.io -u "$${GITHUB_USER}" --password-stdin

# Single-arch push (matches your local docker build)
ghcr-push: docker-build
	@test -n "$(GHCR_OWNER)" || (echo "Set GITHUB_USER or GHCR_OWNER" && exit 1)
	docker tag $(IMAGE):$(TAG) $(GHCR_IMAGE):$(TAG)
	docker tag $(IMAGE):$(TAG) $(GHCR_IMAGE):latest
	docker push $(GHCR_IMAGE):$(TAG)
	docker push $(GHCR_IMAGE):latest
	@echo "Pulled as: $(GHCR_IMAGE):$(TAG)"

# Multi-arch (needs buildx). Example:
#   docker buildx create --use --name multi 2>/dev/null || true
ghcr-push-multi:
	@test -n "$(GHCR_OWNER)" || (echo "Set GITHUB_USER or GHCR_OWNER" && exit 1)
	docker buildx build \
	  --platform $(PLATFORMS) \
	  -t $(GHCR_IMAGE):$(TAG) \
	  -t $(GHCR_IMAGE):latest \
	  --push \
	  .

helm-lint:
	helm lint $(CHART)

helm-template:
	helm template $(RELEASE) $(CHART) \
	  --set cursor.apiKey=cursor_dummy \
	  --set image.repository=$(IMAGE) \
	  --set image.tag=$(TAG)

# Prefer creating a Secret first, then:
#   helm upgrade --install ... --set cursor.existingSecret=cursor-api
helm-install:
	@test -n "$${CURSOR_API_KEY}" || (echo "Set CURSOR_API_KEY first" && exit 1)
	kubectl get ns $(NAMESPACE) >/dev/null 2>&1 || kubectl create ns $(NAMESPACE)
	helm upgrade --install $(RELEASE) $(CHART) \
	  --namespace $(NAMESPACE) \
	  --set image.repository=$(IMAGE) \
	  --set image.tag=$(TAG) \
	  --set-string cursor.apiKey="$${CURSOR_API_KEY}"

helm-upgrade: helm-install

helm-uninstall:
	helm uninstall $(RELEASE) --namespace $(NAMESPACE) || true
