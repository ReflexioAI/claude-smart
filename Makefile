# Release automation for claude-smart.
#
# Usage:
#   make bump VERSION=0.1.1          Update version in all release manifests
#   make release VERSION=0.1.1       Alias for release-npm (claude-smart ships via npm only)
#   make release-npm VERSION=0.1.1   Re-vendor reflexio, bump, commit, tag, publish npm, push
#   make publish                     Publish current version to npm
#   make publish-npm                 npm publish only
#   make publish-dry                 Show what would ship without uploading
#   make package                     Build the npm tarball locally (vendors local reflexio working tree)
#
# claude-smart is distributed exclusively via npm; it is no longer published to
# PyPI (uvx install is unsupported). The reflexio-ai dependency still resolves
# from PyPI, and a release always vendors the current reflexio submodule.
#
# Requires:
#   - npm (logged in, or NPM_TOKEN set)
#   - uv (for standalone lockfile resolution + the plugin venv)
#   - git (for the release flow)

.PHONY: help bump release release-npm publish publish-npm publish-dry package package-desktop \
        vendor-release \
        check-version check-clean check-npm-auth check-reflexio-pin check-reflexio-lock \
        check-vendor-reflexio \
        check-locked-project-version check-standalone-lock relock unskip-worktree

# .claude-plugin/marketplace.json is intentionally absent: it is generated at
# pack time from package.json's version by scripts/generate-marketplace.js (via
# npm's prepack hook), so there is nothing here to bump.
VERSION_FILES := package.json plugin/pyproject.toml \
                 plugin/.claude-plugin/plugin.json plugin/.codex-plugin/plugin.json \
                 README.md
LOCK_FILES    := package-lock.json plugin/uv.lock reflexio.lock.json
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

vendor-release: unskip-worktree ## Re-vendor the current (clean) reflexio submodule + refresh reflexio.lock.json
	@echo "→ vendoring reflexio submodule (must be clean) into plugin/vendor/reflexio"
	@python3 scripts/vendor-reflexio.py --require-clean --write

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

check-locked-project-version: ## Verify plugin/uv.lock claude-smart version matches plugin/pyproject.toml
	@manifest=$$(awk -F'"' '/^version =/{print $$2; exit}' $(PYPROJECT)); \
	  locked=$$(awk '/^name = "claude-smart"$$/{f=1; next} f && /^version =/{gsub(/"/, "", $$3); print $$3; exit}' plugin/uv.lock); \
	  if [ -z "$$manifest" ] || [ -z "$$locked" ]; then \
	    echo "error: could not parse versions (manifest='$$manifest' locked='$$locked')" >&2; exit 1; \
	  fi; \
	  if [ "$$manifest" != "$$locked" ]; then \
	    echo "error: plugin/uv.lock claude-smart version ($$locked) does not match $(PYPROJECT) ($$manifest); the bump step should have synced both" >&2; exit 1; \
	  fi; \
	  echo "→ ok: plugin/uv.lock claude-smart matches $(PYPROJECT) ($$manifest)"

check-standalone-lock: ## Verify plugin/uv.lock resolves cleanly OUTSIDE the enterprise uv workspace (what end users hit)
	@bash scripts/standalone-lock.sh --check

relock: unskip-worktree ## Regenerate plugin/uv.lock as a standalone lockfile (reflexio-ai from PyPI)
	@bash scripts/standalone-lock.sh --write

bump: check-version unskip-worktree ## Rewrite version in all release manifests
	@echo "→ bumping to $(VERSION)"
	@sed -i.bak -E 's/"version": "[^"]+"/"version": "$(VERSION)"/' \
	    package.json plugin/.claude-plugin/plugin.json plugin/.codex-plugin/plugin.json
	@sed -i.bak -E 's/^version = "[^"]+"/version = "$(VERSION)"/' plugin/pyproject.toml
	@python3 -c 'import json, pathlib; p=pathlib.Path("package-lock.json"); data=json.loads(p.read_text()); data["version"]="$(VERSION)"; data["packages"][""]["version"]="$(VERSION)"; p.write_text(json.dumps(data, indent=2) + "\n")'
	@sed -i.bak -E 's|badge/version-[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?-green\.svg|badge/version-$(VERSION)-green.svg|' README.md
	@rm -f package.json.bak plugin/pyproject.toml.bak \
	       plugin/.claude-plugin/plugin.json.bak plugin/.codex-plugin/plugin.json.bak \
	       README.md.bak
	@echo "→ regenerating standalone plugin/uv.lock (resolves reflexio-ai from PyPI)"
	@# plugin/ is a member of the enterprise uv workspace, so `uv lock --project
	@# plugin` writes the PARENT lockfile and leaves plugin/uv.lock's dependency
	@# graph stale (only its version was awk-patched), which makes end-user
	@# `uv sync --locked` fail standalone (see v0.2.38 + lock-drift incidents).
	@# Regenerate it outside the workspace instead; the version comes from the
	@# already-bumped plugin/pyproject.toml. check-standalone-lock guards this.
	@bash scripts/standalone-lock.sh --write
	@echo "→ resulting versions:"
	@grep -HE '("version"|^version)' $(VERSION_FILES)

publish-npm: check-vendor-reflexio check-locked-project-version check-standalone-lock ## Publish the current version to npm
	@echo "→ npm publish"
	npm run build:opencode
	npm publish --access public

publish-dry: unskip-worktree check-vendor-reflexio check-locked-project-version check-standalone-lock ## Show what would be published without uploading
	@echo "→ npm publish --dry-run"
	@npm run build:opencode
	@npm publish --dry-run

package: check-locked-project-version check-standalone-lock ## Build the npm tarball locally (vendors the local reflexio working tree)
	@echo "→ vendoring local reflexio working tree (uncommitted edits included) into plugin/vendor/reflexio"
	@python3 scripts/vendor-reflexio.py --bundle-only --write
	@echo "→ npm pack"
	@npm run build:opencode
	@tarball=$$(npm pack 2>/dev/null | tail -1); \
	  abs=$$(cd "$$(dirname "$$tarball")" && pwd)/$$(basename "$$tarball"); \
	  echo ""; \
	  echo "✓ built $$abs"; \
	  echo ""; \
	  echo "Install locally with one of:"; \
	  echo "  npm install -g $$abs && claude-smart install"; \
	  echo "  npm install -g $$abs && claude-smart install --host codex"; \
	  echo "  npm install -g $$abs && claude-smart install --host opencode"; \
	  echo "  npx --package=$$abs -- claude-smart install"; \
	  echo "  npx --package=$$abs -- claude-smart install --host codex"; \
	  echo "  npx --package=$$abs -- claude-smart install --host opencode"

package-desktop: package ## Build the claude.ai desktop-uploadable plugin zip (Settings -> Plugins -> Upload plugin)
	@bash scripts/build-desktop-plugin.sh --skip-build

publish: publish-npm ## Publish the current version to npm (claude-smart is npm-only)

release: ## Alias for release-npm — claude-smart is distributed via npm only
	@$(MAKE) release-npm VERSION=$(VERSION)

release-npm: check-version check-clean check-npm-auth vendor-release check-reflexio-lock check-reflexio-pin check-vendor-reflexio bump check-locked-project-version ## Re-vendor + bump + commit + tag + publish npm
	@echo "→ committing npm release v$(VERSION)"
	git add $(VERSION_FILES) $(LOCK_FILES)
	git commit -m "Release v$(VERSION)"
	git tag -a v$(VERSION) -m "Release v$(VERSION)"
	@$(MAKE) publish-npm
	@echo "→ pushing commit + tag"
	git push --follow-tags
	@echo "✓ released npm v$(VERSION)"
