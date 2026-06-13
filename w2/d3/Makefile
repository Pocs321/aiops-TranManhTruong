.PHONY: install run run-prod test clean

install:
	uv venv
	uv pip install -r requirements.txt

run:
	uvicorn serve:app --host 0.0.0.0 --port 8000 --reload

run-prod:
	uvicorn serve:app --host 0.0.0.0 --port 8000 --workers 4 --no-access-log

test:
	AIOPS_USE_LLM=false pytest -v tests/

clean:
	find . -name "__pycache__" -type d -exec rm -rf {} +
	rm -rf .pytest_cache
