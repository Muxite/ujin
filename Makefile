# Local equivalents of the CI gates.
.PHONY: test cov bench check install-dev

install-dev:
	pip install -e .[all,dev]

test:
	pytest -q

cov:
	pytest --cov --cov-report=term-missing

bench:
	pytest benchmarks/ --benchmark-only --benchmark-disable-gc -q

check: cov
