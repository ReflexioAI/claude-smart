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
#   make package                     Build the npm tarball locally without publishing
#
# Requires:
#   - npm (logged in, or NPM_TOKEN set)
#   - uv (UV_PUBLISH_TOKEN set for PyPI uploads)
#   - git (for the release flow)

.PHONY: help bump release release-npm publish publish-npm publish-pypi publish-dry package \
        check-version check-clean check-npm-auth check-reflexio-pin check-reflexio-lock \
        check-vendor-reflexio check-pypi-compatible-reflexio \
        check-locked-project-version unskip-worktree

VERSION_FILES := package.json plugin/pyproject.toml \
                 plugin/.claude-plugin/plugin.json plugin/.codex-plugin/plugin.json \
                 .claude-plugin/marketplace.json README.md
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

check-pypi-compatible-reflexio: ## Refuse PyPI publish when this release relies on a generated vendor bundle
	@python3 -c 'import json, pathlib, sys; p=pathlib.Path("reflexio.lock.json"); data=json.loads(p.read_text()) if p.exists() else {}; sys.exit("error: reflexio.lock.json uses source=vendor; publish npm only with `make release-npm VERSION=...`, or run `REFLEXIO_RELEASE_SOURCE=pypi bash scripts/release-with-reflexio.sh` first" if data.get("source") == "vendor" else 0)'

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

bump: check-version unskip-worktree ## Rewrite version in all release manifests
	@echo "→ bumping to $(VERSION)"
	@sed -i.bak -E 's/"version": "[^"]+"/"version": "$(VERSION)"/' \
	    package.json plugin/.claude-plugin/plugin.json plugin/.codex-plugin/plugin.json \
	    .claude-plugin/marketplace.json
	@sed -i.bak -E 's/^version = "[^"]+"/version = "$(VERSION)"/' plugin/pyproject.toml
	@python3 -c 'import json, pathlib; p=pathlib.Path("package-lock.json"); data=json.loads(p.read_text()); data["version"]="$(VERSION)"; data["packages"][""]["version"]="$(VERSION)"; p.write_text(json.dumps(data, indent=2) + "\n")'
	@sed -i.bak -E 's|badge/version-[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?-green\.svg|badge/version-$(VERSION)-green.svg|' README.md
	@rm -f package.json.bak plugin/pyproject.toml.bak \
	       plugin/.claude-plugin/plugin.json.bak plugin/.codex-plugin/plugin.json.bak \
	       .claude-plugin/marketplace.json.bak README.md.bak
	@echo "→ refreshing uv lockfile (resolves reflexio-ai from PyPI)"
	@uv lock --refresh-package reflexio-ai --project plugin
	@# Force-sync the workspace project's own version in plugin/uv.lock.
	@# uv does NOT re-lock the editable project's version on a plain `uv lock`
	@# (and silently no-ops --refresh-package <project>), and when this tree is
	@# absorbed into a parent uv workspace, `uv lock --project plugin` writes to
	@# the parent's lockfile rather than plugin/uv.lock at all. Either path
	@# leaves plugin/uv.lock pinned to the old version, which makes
	@# `uv sync --locked` silently drop the project from the install set on
	@# end-user machines (see v0.2.38 incident).
	@awk -v new='$(VERSION)' '\
	  /^name = "claude-smart"$$/{f=1; print; next} \
	  f && /^version = /{sub(/"[^"]+"/, "\"" new "\""); f=0} \
	  {print}' plugin/uv.lock > plugin/uv.lock.tmp && mv plugin/uv.lock.tmp plugin/uv.lock
	@echo "→ resulting versions:"
	@grep -HE '("version"|^version)' $(VERSION_FILES)

publish-npm: check-vendor-reflexio check-locked-project-version ## Publish the current version to npm
	@echo "→ npm publish"
	npm run build:opencode
	npm publish --access public

publish-pypi: check-pypi-compatible-reflexio unskip-worktree ## Build and publish the current version to PyPI
	@echo "→ uv build + uv publish"
	rm -rf plugin/dist/
	uv build --project plugin --out-dir plugin/dist
	uv publish --project plugin plugin/dist/*

publish-dry: unskip-worktree check-vendor-reflexio check-locked-project-version ## Show what would be published without uploading
	@echo "→ npm publish --dry-run"
	@npm run build:opencode
	@npm publish --dry-run
	@echo ""
	@echo "→ uv build (dry: inspect plugin/dist/ manually)"
	rm -rf plugin/dist/
	uv build --project plugin
	@ls -la plugin/dist/

package: check-vendor-reflexio check-locked-project-version ## Build the npm tarball locally without publishing
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

publish: check-pypi-compatible-reflexio publish-npm publish-pypi ## Publish to both npm and PyPI

release: check-version check-clean check-npm-auth check-reflexio-lock check-reflexio-pin check-pypi-compatible-reflexio bump check-locked-project-version ## Bump + commit + tag + publish + push
	@echo "→ committing release v$(VERSION)"
	git add $(VERSION_FILES) $(LOCK_FILES)
	git commit -m "Release v$(VERSION)"
	git tag -a v$(VERSION) -m "Release v$(VERSION)"
	@$(MAKE) publish
	@echo "→ pushing commit + tag"
	git push --follow-tags
	@echo "✓ released v$(VERSION)"

release-npm: check-version check-clean check-npm-auth check-reflexio-lock check-reflexio-pin check-vendor-reflexio bump check-locked-project-version ## Bump + commit + tag + publish npm only
	@echo "→ committing npm release v$(VERSION)"
	git add $(VERSION_FILES) $(LOCK_FILES)
	git commit -m "Release v$(VERSION)"
	git tag -a v$(VERSION) -m "Release v$(VERSION)"
	@$(MAKE) publish-npm
	@echo "→ pushing commit + tag"
	git push --follow-tags
	@echo "✓ released npm v$(VERSION)"
