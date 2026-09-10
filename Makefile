.PHONY: test lint
test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v
lint:
	python3 -m compileall -q src tests
