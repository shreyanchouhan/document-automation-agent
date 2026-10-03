.PHONY: install corpus run queue evals test console api image clean

install:
	python -m venv .venv && .venv/bin/pip install -r requirements.txt

corpus:
	.venv/bin/python data/generate_corpus.py

run:
	.venv/bin/python -m docagent.cli run --verbose

queue:
	.venv/bin/python -m docagent.cli queue

evals:
	.venv/bin/python evals/run_eval.py --strict

test:
	.venv/bin/python -m pytest -q

console:
	.venv/bin/python -m streamlit run console/app.py

api:
	.venv/bin/python -m uvicorn docagent.api:app --reload --port 8080

image:
	docker build -f deploy/Dockerfile -t document-automation-agent:dev .

clean:
	rm -rf data/corpus data/docagent.db data/baseline.json data/lakehouse evals/results.json
	find . -name __pycache__ -type d -exec rm -rf {} +
