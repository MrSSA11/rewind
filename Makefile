.PHONY: help install signoz demo run test health clean

help:
	@echo "Rewind - a rewind button for AI agents"
	@echo ""
	@echo "  make install   install Python dependencies"
	@echo "  make signoz    start self-hosted SigNoz with Foundry"
	@echo "  make demo      seed the buggy demo run, then start the app"
	@echo "  make run       start the Rewind app on port 8000"
	@echo "  make test      run the test suite"
	@echo "  make health    check which telemetry backend is serving"
	@echo "  make clean     remove local telemetry mirror and caches"

install:
	pip install -r requirements.txt

signoz:
	cd signoz && foundry up

demo:
	FAKE_LLM=1 python -m demo_agent.seed
	$(MAKE) run

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test:
	FAKE_LLM=1 REWIND_DISABLE_OTEL=1 REWIND_BACKEND=mirror pytest -q

health:
	curl -s localhost:8000/healthz

clean:
	rm -rf .rewind __pycache__ .pytest_cache
	find . -name "__pycache__" -type d -exec rm -rf {} +
