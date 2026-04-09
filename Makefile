.PHONY: build release clean

VERSION ?= $(shell grep 'version =' pyproject.toml | sed 's/.*"\(.*\)".*/\1/')

build:
	@echo "Building package version $(VERSION)..."
	python3 -m build || pipx run build
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
	twine upload dist/* || python3 -m twine upload dist/* || pipx run twine upload dist/*

clean:
	rm -rf dist/ build/ *.egg-info
