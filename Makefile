.PHONY: test scan build check models
test:
	PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
scan:
	python3 scripts/scan_public.py
check: test scan
build:
	docker build -t vector-puck:local .
models:
	python3 scripts/download_models.py
