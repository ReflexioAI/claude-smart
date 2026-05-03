# Release automation for claude-smart.
#
# Usage:
#   make bump VERSION=0.1.1          Update version in all 4 manifests
#   make release VERSION=0.1.1       Bump, commit, tag v0.1.1, publish, push
#   make publish                     Publish current version to npm + PyPI
#   make publish-npm                 npm publish only
#   make publish-pypi                uv build + uv publish only
#   make publish-dry                 Show what would ship without uploading
#
# Requires:
#   - npm (logged in, or NPM_TOKEN set)
#   - uv (UV_PUBLISH_TOKEN set for PyPI uploads)
#   - git (for the release flow)

.PHONY: help bump release publish publish-npm publish-pypi publish-dry \
        check-version check-clean check-npm-auth ensure-remote-reflexio unskip-worktree

VERSION_FILES := package.json plugin/pyproject.toml \
                 plugin/.claude-plugin/plugin.json .claude-plugin/marketplace.json \
                 README.md
LOCK_FILES    := plugin/uv.lock
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

ensure-remote-reflexio: ## Ensure [tool.uv.sources] is commented out so published wheels resolve reflexio-ai from PyPI (see scripts/setup-local-dev.sh to re-enable for dev)
	@echo "→ ensuring [tool.uv.sources] override is commented out in $(PYPROJECT)"
	@sed -i.bak -E \
	    -e 's|^\[tool\.uv\.sources\]$$|# [tool.uv.sources]|' \
	    -e 's|^(reflexio-ai = \{ path = .*)$$|# \1|' \
	    $(PYPROJECT)
	@rm -f $(PYPROJECT).bak
	@if grep -qE '^\[tool\.uv\.sources\]|^reflexio-ai = \{ path =' $(PYPROJECT); then \
	  echo "error: [tool.uv.sources] block in $(PYPROJECT) is still active after sed" >&2; \
	  exit 1; \
	fi

bump: check-version unskip-worktree ensure-remote-reflexio ## Rewrite version in all 4 manifests
	@echo "→ bumping to $(VERSION)"
	@sed -i.bak -E 's/"version": "[^"]+"/"version": "$(VERSION)"/' \
	    package.json plugin/.claude-plugin/plugin.json .claude-plugin/marketplace.json
	@sed -i.bak -E 's/^version = "[^"]+"/version = "$(VERSION)"/' plugin/pyproject.toml
	@sed -i.bak -E 's|badge/version-[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?-green\.svg|badge/version-$(VERSION)-green.svg|' README.md
	@rm -f package.json.bak plugin/pyproject.toml.bak \
	       plugin/.claude-plugin/plugin.json.bak .claude-plugin/marketplace.json.bak \
	       README.md.bak
	@echo "→ refreshing uv lockfile (resolves reflexio-ai from PyPI)"
	@uv lock --project plugin
	@echo "→ resulting versions:"
	@grep -HE '("version"|^version)' $(VERSION_FILES)

publish-npm: ## Publish the current version to npm
	@echo "→ npm publish"
	npm publish --access public

publish-pypi: unskip-worktree ensure-remote-reflexio ## Build and publish the current version to PyPI
	@echo "→ uv build + uv publish"
	rm -rf plugin/dist/
	uv build --project plugin
	uv publish --project plugin plugin/dist/*

publish-dry: unskip-worktree ensure-remote-reflexio ## Show what would be published without uploading
	@echo "→ npm publish --dry-run"
	@npm publish --dry-run
	@echo ""
	@echo "→ uv build (dry: inspect plugin/dist/ manually)"
	rm -rf plugin/dist/
	uv build --project plugin
	@ls -la plugin/dist/

publish: publish-npm publish-pypi ## Publish to both npm and PyPI

release: check-version check-clean check-npm-auth bump ## Bump + commit + tag + publish + push
	@echo "→ committing release v$(VERSION)"
	git add $(VERSION_FILES) $(LOCK_FILES)
	git commit -m "Release v$(VERSION)"
	git tag -a v$(VERSION) -m "Release v$(VERSION)"
	@$(MAKE) publish
	@echo "→ pushing commit + tag"
	git push --follow-tags
	@echo "✓ released v$(VERSION)"
