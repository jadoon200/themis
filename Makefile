.PHONY: env install lint format typecheck test check demo-build review execute grain lineage suggest-tests warm eval clean up down migrate api worker record-cassette

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

# Column-level lineage coverage: how much of the project resolved, and how much did not.
lineage:
	PYTHONPATH=src python -m themis.cli lineage --project demo_project

# The uniqueness tests the project never declared, as a schema.yml fragment.
suggest-tests:
	PYTHONPATH=src python -m themis.cli suggest-tests --project demo_project --yaml

# Compile main into the manifest cache, so no review pays for the base compile.
warm:
	PYTHONPATH=src python -m themis.cli cache --warm main --project demo_project

# Mutation eval: per-family precision/recall against the execution oracle.
eval:
	PYTHONPATH=src python -m themis.cli eval --mutations all --project demo_project

# --- infrastructure ---

# Postgres via Docker Compose (host port 5436 — coexists with the siblings), then migrate.
up:
	docker compose up -d db && sleep 3 && $(MAKE) migrate

down:
	docker compose down

# Defaults to SQLite; point THEMIS_DATABASE_URL at Postgres for the service.
migrate:
	PYTHONPATH=src alembic upgrade head

# The API queues reviews and serves results. It never runs one itself.
api:
	PYTHONPATH=src uvicorn themis.api.app:app --host 127.0.0.1 --port 8040 --reload

# Workers claim queued runs and execute the same pipeline the CLI does.
worker:
	PYTHONPATH=src python -m themis.worker

# Re-record the model responses the replay test uses. Needs a real Ollama; run after
# changing any prompt, because a cassette key includes the prompt it answered.
record-cassette:
	PYTHONPATH=src python scripts/record_cassette.py

clean:
	rm -rf demo_project/target demo_project/logs demo_project/*.duckdb .themis
