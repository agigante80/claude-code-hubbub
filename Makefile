VENV := .venv
PY := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest
DEV_REQS := requirements-dev.txt
RUNTIME_REQS := skills/talk/requirements.txt

# Second venv, built explicitly from the *system* `python3` — the interpreter
# the shipped monitors actually run under. See `test-system` below for why
# this is not paranoia.
SYS_VENV := .venv-system
SYS_PYTEST := $(SYS_VENV)/bin/pytest

# Sentinel file marks "deps are up-to-date with the reqs files". `make`
# rebuilds it whenever either reqs file is newer (or it's missing), so
# pulling new deps just means re-running `make test`.
DEPS_STAMP := $(VENV)/.deps-stamp
SYS_DEPS_STAMP := $(SYS_VENV)/.deps-stamp

.PHONY: test test-fast test-system test-both coverage versions clean help
.DEFAULT_GOAL := help

# `make -j2 test-both` would otherwise run two pytest sessions at once. That
# is survivable for the suite proper since fork #17, but NOT for the canary
# tests in test_auto_start.py: they capture and restore the *real*
# monitors/monitors.json, so two sessions can interleave capture and restore
# and leave the tracked file mutated. The help text promises "sequentially";
# this is what makes that true rather than aspirational.
.NOTPARALLEL:

help:
	@echo "Targets (all run inside a project-local venv; system Python is never modified):"
	@echo "  make test         Run the full pytest suite in $(VENV)."
	@echo "  make test-fast    Skip subprocess-spawning tests (@pytest.mark.slow)."
	@echo "  make test-system  Run the full suite under the SYSTEM python3 in $(SYS_VENV)."
	@echo "  make test-both    Both of the above, sequentially. Use before shipping."
	@echo "  make coverage     Full suite under coverage; fails below the floor in .coveragerc."
	@echo "  make versions     Show which interpreter each venv resolves to."
	@echo "  make clean        Remove both venvs."
	@echo ""
	@echo "Why test-system exists: with uv installed, $(VENV) gets uv's Python,"
	@echo "which is not necessarily the python3 that runs the shipped monitors."
	@echo "A green 'make test' alone does not prove the shipped code is green."

test: $(DEPS_STAMP)
	$(PYTEST) -q

test-fast: $(DEPS_STAMP)
	$(PYTEST) -q -m "not slow"

# fork #24. `make test` builds $(VENV) with uv when uv is present, and uv
# supplies its own Python — 3.14 on this machine — while the monitors CC
# spawns run whatever `python3` resolves to, 3.12 here. That is not a
# theoretical gap: `Path.resolve()` raises RuntimeError on a symlink loop
# under 3.12 and silently returns the link under 3.14, which hid a startup
# crash in every entry-point from `make test` until fork #19 went looking.
#
# Deliberately never uses uv, even when available: using the system
# interpreter IS the point of this target.
test-system: $(SYS_DEPS_STAMP)
	@echo "Running under $$($(SYS_VENV)/bin/python -V) (system python3)"
	$(SYS_PYTEST) -q

test-both: test test-system

# fork: coverage was never measured on this repo until it was asked for, and
# the first measurement was wrong in a way worth preventing permanently.
#
# The subprocess tests are most of the integration value, and their children
# are separate processes. coverage only sees them if `coverage.process_startup`
# runs at interpreter start, which needs a .pth in site-packages — so this
# target installs one into $(VENV) (regenerable, gitignored) rather than
# expecting anyone to remember. Without it `auto_start.py` and `doctor.py`
# report 0% and the total reads 68% instead of 82%.
COV_PTH = $(shell $(VENV)/bin/python -c "import site; print(site.getsitepackages()[0])" 2>/dev/null)/zz-hubbub-coverage-subprocess.pth

coverage: $(DEPS_STAMP)
	@echo "import coverage; coverage.process_startup()" > "$(COV_PTH)"
	@rm -f .coverage .coverage.*
	@COVERAGE_PROCESS_START=$(CURDIR)/.coveragerc \
		$(VENV)/bin/python -m coverage run -m pytest -q
	@$(VENV)/bin/python -m coverage combine
	@$(VENV)/bin/python -m coverage report
	@rm -f "$(COV_PTH)"

versions:
	@printf '%-16s ' "$(VENV):"; \
	  [ -x $(VENV)/bin/python ] && $(VENV)/bin/python -V || echo "(not built)"
	@printf '%-16s ' "$(SYS_VENV):"; \
	  [ -x $(SYS_VENV)/bin/python ] && $(SYS_VENV)/bin/python -V || echo "(not built)"
	@printf '%-16s ' "system python3:"; python3 -V

clean:
	rm -rf $(VENV) $(SYS_VENV)
	rm -f .coverage .coverage.*

$(DEPS_STAMP): $(DEV_REQS) $(RUNTIME_REQS)
	@if command -v uv >/dev/null 2>&1; then \
		echo "Bootstrapping $(VENV) with uv..."; \
		uv venv $(VENV); \
		uv pip install -p $(VENV) -r $(DEV_REQS); \
	else \
		echo "Bootstrapping $(VENV) with python3 -m venv (uv not found)..."; \
		python3 -m venv $(VENV); \
		$(VENV)/bin/pip install -r $(DEV_REQS); \
	fi
	@touch $(DEPS_STAMP)

$(SYS_DEPS_STAMP): $(DEV_REQS) $(RUNTIME_REQS)
	@echo "Bootstrapping $(SYS_VENV) with $$(python3 -V) (system python3, never uv)..."
	@python3 -m venv $(SYS_VENV) || { \
		echo ""; \
		echo "Could not create a venv with the system python3."; \
		echo "On Debian/Ubuntu: apt install python3-venv"; \
		exit 1; \
	}
	$(SYS_VENV)/bin/pip install -q -r $(DEV_REQS)
	@touch $(SYS_DEPS_STAMP)
