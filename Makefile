PY := python
export PYTHONPATH := src

.PHONY: install run eval bench build clean

install:
	pip install -e .

run:
	$(PY) -m agent.cli --in data/questions.jsonl --out results.jsonl

eval:
	$(PY) eval/run_eval.py

bench:
	$(PY) -m agent.cli --in data/questions.jsonl --out results.jsonl --bench

build:
	$(PY) -m agent.build_facts --store data/store.json --out data/facts.json

clean:
	find . -name "__pycache__" -type d -exec rm -rf {} +