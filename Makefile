REGISTRY  := zot.lan
IMAGE     := homereef-coralforge
TAG       := latest
NAMESPACE := kube-idle
RELEASE   := coralforge
CHART     := helm

.PHONY: build push deploy helm-upgrade lint

build:
	@MANIFEST=$(IMAGE)-manifest:$(TAG); \
	podman manifest rm "$$MANIFEST" 2>/dev/null || true; \
	podman buildx build \
		--platform linux/amd64,linux/arm64 \
		--manifest "$$MANIFEST" \
		-f Containerfile \
		.

push: build
	@MANIFEST=$(IMAGE)-manifest:$(TAG); \
	podman manifest push --tls-verify=false --all "$$MANIFEST" \
		"docker://$(REGISTRY)/$(IMAGE):$(TAG)"

helm-upgrade:
	helm upgrade --install $(RELEASE) $(CHART) \
		--namespace $(NAMESPACE) \
		--create-namespace \
		-f helm/values.local.yaml

deploy: push helm-upgrade
	kubectl rollout restart deployment/$(RELEASE) -n $(NAMESPACE)
	kubectl rollout status deployment/$(RELEASE) -n $(NAMESPACE)

lint:
	helm lint $(CHART)
