SHELL := /bin/bash
.DEFAULT_GOAL := check
.NOTPARALLEL:
export ATR_REQUIRE_BROWSER := 1
export EXTERNAL_MODEL_ENABLED := false
export SSH_ENABLED := false
export LITELLM_LOCAL_MODEL_COST_MAP := True
.PHONY: deps init lint test build e2e features check verify serve
deps:
	uv sync --locked --group dev
	uv lock --check
	cd frontend && npm ci
init:
	bash scripts/init.sh --no-serve
lint:
	node scripts/lint.mjs
	uv run --locked ruff check .
test: build
	node --test tests/*.test.mjs
	mkdir -p test-results
	uv run --locked pytest -q --basetemp=test-results/make-temp --junitxml=test-results/make.xml
build:
	cd frontend && npm run build
	node scripts/site-build.mjs
e2e:
	node scripts/site-e2e.mjs all
features:
	node scripts/features.mjs recheck
check: deps lint test build e2e features
verify:
	@test -n "$(F)"
	bash scripts/verify-feature.sh $(F)
serve:
	node scripts/site-build.mjs
	node scripts/serve.mjs dist-site 8080
