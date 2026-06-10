# Local equivalents of the CI gates.
.PHONY: test cov bench check install-dev

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
