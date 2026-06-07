PYTHON ?= python3
PIP_INSTALL = $(PYTHON) -m pip install -e './launcher[dev]'

.PHONY: lint test run-mock

lint:
	$(PIP_INSTALL)
	$(PYTHON) -m ruff check launcher/src launcher/tests
	$(PYTHON) -m black --check launcher/src launcher/tests

test:
	$(PIP_INSTALL)
	$(PYTHON) -m pytest launcher/tests

run-mock:
	$(PIP_INSTALL)
	chipbit-launcher --mock-reader