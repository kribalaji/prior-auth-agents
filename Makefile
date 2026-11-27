.PHONY: setup data test demo payer lint

setup:
	uv sync --extra dev

data:            ## regenerate the fictional FHIR bundles (already committed)
	uv run python data_gen/build_fhir.py

test:
	uv run pytest -q

demo:            ## run all 12 scenarios end to end (no API key needed)
	uv run priorauth demo

payer:           ## serve the payer A2A agent on :9100 (agent card at /.well-known/agent-card.json)
	uv run priorauth serve-payer --port 9100

lint:
	uv run ruff check src tests data_gen
