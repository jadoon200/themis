.PHONY: env install lint format typecheck test check demo-build review execute grain eval clean

# One-time: create the conda env, then `conda activate themis`
env:
	conda create -y -n themis python=3.12 pip

# Run inside the activated themis env.
install:
	python -m pip install -r requirements-dev.txt
	python -m pip install -e .

lint:
	PYTHONPATH=src ruff check src tests

format:
	PYTHONPATH=src ruff format src tests

typecheck:
	PYTHONPATH=src mypy src/themis

test:
	PYTHONPATH=src python -m pytest -q

check: lint typecheck test

# --- demo project (DuckDB, no warehouse, no credentials) ---

# Seed and build the synthetic financial dbt project.
demo-build:
	cd demo_project && dbt seed && dbt build

# --- the product ---

# Full review of the working tree against main. Add --execute for run-backed evidence.
review:
	PYTHONPATH=src python -m themis.cli review --base main --head HEAD --project demo_project

# Stage 3 only: build base + head and diff the actual results.
execute:
	PYTHONPATH=src python -m themis.cli execute --base main --head HEAD --project demo_project --explain

# Show the derived grain for every model and which source produced it.
grain:
	PYTHONPATH=src python -m themis.cli grain --project demo_project --explain

# Mutation eval: per-family precision/recall against the execution oracle.
eval:
	PYTHONPATH=src python -m themis.cli eval --mutations all --project demo_project

clean:
	rm -rf demo_project/target demo_project/logs demo_project/*.duckdb .themis
