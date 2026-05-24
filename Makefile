# Release automation for claude-smart.
#
# Usage:
#   make bump VERSION=0.1.1          Update version in all release manifests
#   make release VERSION=0.1.1       Bump, commit, tag v0.1.1, publish, push
#   make release-npm VERSION=0.1.1   Bump, commit, tag, publish npm only
#   make publish                     Publish current version to npm + PyPI
#   make publish-npm                 npm publish only
#   make publish-pypi                uv build + uv publish only
#   make publish-dry                 Show what would ship without uploading
#
# Requires:
#   - npm (logged in, or NPM_TOKEN set)
#   - uv (UV_PUBLISH_TOKEN set for PyPI uploads)
#   - git (for the release flow)

.PHONY: help bump release release-npm publish publish-npm publish-pypi publish-dry \
        check-version check-clean check-npm-auth check-reflexio-pin check-reflexio-lock \
        check-vendor-reflexio check-pypi-compatible-reflexio ensure-remote-reflexio \
        doctor-reflexio unskip-worktree

VERSION_FILES := package.json plugin/pyproject.toml \
                 plugin/.claude-plugin/plugin.json plugin/.codex-plugin/plugin.json \
                 .claude-plugin/marketplace.json README.md
LOCK_FILES    := plugin/uv.lock reflexio.lock.json
PYPROJECT     := plugin/pyproject.toml

help:
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z_-]+:.*##/{printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

check-version:
ifndef VERSION
	$(error VERSION is required, e.g. make bump VERSION=0.1.1)
endif
	@printf '%s' '$(VERSION)' | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$$' \
	  || { echo "error: VERSION '$(VERSION)' is not valid semver" >&2; exit 1; }

check-clean:
	@git diff --quiet && git diff --cached --quiet \
	  || { echo "error: working tree is dirty — commit or stash first" >&2; exit 1; }

check-reflexio-pin: ## Verify the reflexio-ai version pinned in plugin/pyproject.toml exists on PyPI
	@pin=$$(grep -oE '"reflexio-ai>=[0-9][^"]*"' $(PYPROJECT) | sed -E 's/.*reflexio-ai>=([0-9.]+).*/\1/' | head -1); \
	  if [ -z "$$pin" ]; then \
	    echo "error: could not parse reflexio-ai pin from $(PYPROJECT)" >&2; exit 1; \
	  fi; \
	  echo "→ verifying reflexio-ai $$pin exists on PyPI"; \
	  status=$$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/reflexio-ai/$$pin/json"); \
	  if [ "$$status" != "200" ]; then \
	    echo "error: reflexio-ai $$pin not found on PyPI (HTTP $$status); publish reflexio first or fix the pin in $(PYPROJECT)" >&2; \
	    exit 1; \
	  fi; \
	  echo "→ ok: reflexio-ai $$pin is on PyPI"

check-reflexio-lock: ## Verify plugin/pyproject.toml matches reflexio.lock.json
	@python3 scripts/check-reflexio-lock.py

check-vendor-reflexio: ## Verify generated Reflexio vendor bundle exists when the lock requires it
	@python3 scripts/check-reflexio-lock.py --check-vendor

check-pypi-compatible-reflexio: ## Refuse PyPI publish when this release relies on a generated vendor bundle
	@python3 -c 'import json, pathlib, sys; p=pathlib.Path("reflexio.lock.json"); data=json.loads(p.read_text()) if p.exists() else {}; sys.exit("error: reflexio.lock.json uses source=vendor; publish npm only with `make release-npm VERSION=...`, or run `REFLEXIO_RELEASE_SOURCE=pypi bash scripts/release-with-reflexio.sh` first" if data.get("source") == "vendor" else 0)'

doctor-reflexio: ## Show whether plugin/.venv imports local, vendor, or PyPI Reflexio
	@python3 scripts/doctor-reflexio.py

check-npm-auth: ## Verify npm auth via NPM_TOKEN or `npm whoami`; fail if neither is available
	@if [ -n "$$NPM_TOKEN" ]; then \
	  echo "→ npm: NPM_TOKEN is set"; \
	elif npm whoami >/dev/null 2>&1; then \
	  echo "→ npm: logged in as $$(npm whoami)"; \
	else \
	  echo "error: not authenticated via npm whoami and NPM_TOKEN is not set; set NPM_TOKEN for CI or run npm login locally" >&2; \
	  exit 1; \
	fi

unskip-worktree: ## Clear skip-worktree on plugin/pyproject.toml and plugin/uv.lock so release edits land in git
	@echo "→ clearing skip-worktree on $(PYPROJECT) $(LOCK_FILES)"
	@git update-index --no-skip-worktree $(PYPROJECT) $(LOCK_FILES) 2>/dev/null || true

ensure-remote-reflexio: ## Ensure published wheels resolve reflexio-ai from PyPI, not a local path
	@echo "→ ensuring $(PYPROJECT) resolves reflexio-ai from PyPI"
	@if grep -qE '^\[tool\.uv\.sources\]|reflexio-ai = \{ path =|file://' $(PYPROJECT); then \
	  echo "error: local reflexio-ai source found in $(PYPROJECT); use scripts/use-local-reflexio.sh for editable local dev" >&2; \
	  exit 1; \
	fi

bump: check-version unskip-worktree ensure-remote-reflexio ## Rewrite version in all release manifests
	@echo "→ bumping to $(VERSION)"
	@sed -i.bak -E 's/"version": "[^"]+"/"version": "$(VERSION)"/' \
	    package.json plugin/.claude-plugin/plugin.json plugin/.codex-plugin/plugin.json \
	    .claude-plugin/marketplace.json
	@sed -i.bak -E 's/^version = "[^"]+"/version = "$(VERSION)"/' plugin/pyproject.toml
	@sed -i.bak -E 's|badge/version-[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?-green\.svg|badge/version-$(VERSION)-green.svg|' README.md
	@rm -f package.json.bak plugin/pyproject.toml.bak \
	       plugin/.claude-plugin/plugin.json.bak plugin/.codex-plugin/plugin.json.bak \
	       .claude-plugin/marketplace.json.bak README.md.bak
	@echo "→ refreshing uv lockfile (resolves reflexio-ai from PyPI)"
	@uv lock --refresh-package reflexio-ai --project plugin
	@echo "→ resulting versions:"
	@grep -HE '("version"|^version)' $(VERSION_FILES)

publish-npm: check-vendor-reflexio ## Publish the current version to npm
	@echo "→ npm publish"
	npm publish --access public

publish-pypi: check-pypi-compatible-reflexio unskip-worktree ensure-remote-reflexio ## Build and publish the current version to PyPI
	@echo "→ uv build + uv publish"
	rm -rf plugin/dist/
	uv build --project plugin
	uv publish --project plugin plugin/dist/*

publish-dry: unskip-worktree ensure-remote-reflexio check-vendor-reflexio ## Show what would be published without uploading
	@echo "→ npm publish --dry-run"
	@npm publish --dry-run
	@echo ""
	@echo "→ uv build (dry: inspect plugin/dist/ manually)"
	rm -rf plugin/dist/
	uv build --project plugin
	@ls -la plugin/dist/

publish: check-pypi-compatible-reflexio publish-npm publish-pypi ## Publish to both npm and PyPI

release: check-version check-clean check-npm-auth check-reflexio-lock check-reflexio-pin check-pypi-compatible-reflexio bump ## Bump + commit + tag + publish + push
	@echo "→ committing release v$(VERSION)"
	git add $(VERSION_FILES) $(LOCK_FILES)
	git commit -m "Release v$(VERSION)"
	git tag -a v$(VERSION) -m "Release v$(VERSION)"
	@$(MAKE) publish
	@echo "→ pushing commit + tag"
	git push --follow-tags
	@echo "✓ released v$(VERSION)"

release-npm: check-version check-clean check-npm-auth check-reflexio-lock check-reflexio-pin check-vendor-reflexio bump ## Bump + commit + tag + publish npm only
	@echo "→ committing npm release v$(VERSION)"
	git add $(VERSION_FILES) $(LOCK_FILES)
	git commit -m "Release v$(VERSION)"
	git tag -a v$(VERSION) -m "Release v$(VERSION)"
	@$(MAKE) publish-npm
	@echo "→ pushing commit + tag"
	git push --follow-tags
	@echo "✓ released npm v$(VERSION)"
