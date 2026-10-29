PYTHON ?= python3
DIST   ?= dist
PYZ    := $(DIST)/btsum.pyz

.PHONY: all lint format test zipapp clean

all: lint test zipapp

lint:
	ruff check .

format:
	ruff format .

test:
	$(PYTHON) -m pytest

zipapp:
	$(PYTHON) devscripts/make_zipapp.py $(PYZ)

clean:
	rm -rf $(DIST) build *.egg-info .pytest_cache .ruff_cache .mypy_cache
