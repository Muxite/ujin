# Local equivalents of the CI gates.
.PHONY: test cov bench check install-dev gate cov-floor

install-dev:
	pip install -e .[all,dev]

test:
	pytest -q

cov:
	pytest --cov --cov-report=term-missing

bench:
	pytest benchmarks/ -q --no-cov --benchmark-disable-gc

bench-record:
	UJIN_BENCH_RECORD=1 pytest benchmarks/ -q --no-cov --benchmark-disable-gc

check: cov

# Full merge gate — what the autonomous orchestrator runs before every merge, and
# what a human should run before pushing. Coverage (incl. tests) then benchmarks.
gate: cov bench

# Print the current total coverage % (used by the orchestrator's ratchet).
cov-floor:
	@pytest --cov --cov-report=term-missing -q 2>/dev/null | awk '/^TOTAL/ {print $$NF}'
