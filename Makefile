.DEFAULT_GOAL := help
.PHONY: help install signoz demo run test health doctor clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install Python dependencies
	pip install -r requirements.txt

signoz: ## Start self-hosted SigNoz with Foundry (UI on :8080)
	cd signoz && PATH="$$HOME/.local/bin:$$PATH" foundryctl cast -f casting.yaml

demo: ## Seed the broken demo run, then start Rewind on :8000
	FAKE_LLM=1 python -m demo_agent.seed
	$(MAKE) run

run: ## Start the Rewind web app on :8000
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test: ## Run the test suite (no network, no API keys)
	FAKE_LLM=1 REWIND_DISABLE_OTEL=1 REWIND_BACKEND=mirror pytest -q

health: ## Check whether Rewind can reach SigNoz
	@curl -s localhost:8000/healthz | python -m json.tool || echo "Rewind is not running. Try: make run"

doctor: ## Diagnose telemetry: where the mirror is and what is in it
	@python -c "import json,os;\
from rewind_sdk import mirror;\
p=mirror.path();\
r=mirror.read_all();\
t={};\
[t.setdefault(x.get('trace_id','?'),[0,0]) for x in r];\
[t[x.get('trace_id','?')].__setitem__(0 if x.get('type')=='span' else 1, t[x.get('trace_id','?')][0 if x.get('type')=='span' else 1]+1) for x in r];\
print('cwd         :', os.getcwd());\
print('mirror path :', p.resolve());\
print('exists      :', p.exists());\
print('records     :', len(r));\
print('backend     :', os.getenv('REWIND_BACKEND','auto'));\
print('traces      :', len(t));\
[print('   ', k, '->', v[0], 'spans,', v[1], 'envelopes') for k,v in t.items()]"

clean: ## Remove the local telemetry mirror and caches
	rm -rf .rewind .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
