.PHONY: build release clean

VERSION ?= $(shell grep 'version =' pyproject.toml | sed 's/.*"\(.*\)".*/\1/')

build:
	@echo "Building package version $(VERSION)..."
	pipx install -e . --force

release:
	@echo "Releasing package..."
	@if [ -z "$(V)" ]; then \
		python3 scripts/bump_version.py; \
	else \
		python3 scripts/bump_version.py $(V); \
	fi
	@$(MAKE) build
	@echo "Publishing to PyPI..."
	twine upload dist/*
	@echo "Creating git tag..."
	git add pyproject.toml build_q/__init__.py
	git commit -m "chore: bump version to $$(grep 'version =' pyproject.toml | sed 's/.*\"\(.*\)\".*/\1/')"
	git tag v$$(grep 'version =' pyproject.toml | sed 's/.*\"\(.*\)\".*/\1/')
	git push origin main --tags

clean:
	rm -rf dist/ build/ *.egg-info
