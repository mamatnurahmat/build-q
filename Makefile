.PHONY: build install release clean

VERSION ?= $(shell grep 'version =' pyproject.toml | sed 's/.*"\(.*\)".*/\1/')

## Build sdist + wheel into dist/
build:
	@echo "Building package version $(VERSION)..."
	python3 -m build 2>/dev/null || pipx run --spec build pyproject-build

## Install locally via pipx (editable)
install:
	pipx install -e . --force

## Bump version, build, and publish to PyPI
release: clean
	@echo "Releasing package..."
	@if [ -z "$(V)" ]; then \
		python3 scripts/bump_version.py; \
	else \
		python3 scripts/bump_version.py $(V); \
	fi
	@$(MAKE) build
	@$(MAKE) install
	@echo "Publishing to PyPI..."
	@pipx run twine upload --skip-existing dist/*

clean:
	rm -rf dist/ build/ *.egg-info
