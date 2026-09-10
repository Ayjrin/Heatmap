export PYTHONDONTWRITEBYTECODE = 1
export PYTHONPATH := $(CURDIR)/src
PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PORT ?= 8000

.PHONY: help setup assets serve demo smoke small full crawl build status test verify docker-up aws-plan
help:
	@echo "make setup    install local dependencies and pinned map assets"
	@echo "make serve    run the app and collection API on localhost:$(PORT)"
	@echo "make smoke | small | full    collect Riot games (bounded, bounded, refreshed universe)"
	@echo "make test     run isolated Python and production JavaScript tests"
	@echo "make verify   validate the currently published dataset"

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install -r requirements.txt
	.venv/bin/python scripts/fetch_assets.py

assets:
	$(PY) scripts/fetch_assets.py

serve:
	$(PY) scripts/dev_server.py --port $(PORT)

demo: serve

smoke:
	$(PY) -m proleague.pipeline --mode smoke --local

small:
	$(PY) -m proleague.pipeline --mode small --local

full:
	$(PY) -m proleague.pipeline --mode full --local

crawl: full

build:
	$(PY) scripts/build_dataset.py

status:
	$(PY) -m proleague.pipeline --status --local

test:
	$(PY) -m pytest tests/ infra/control/ -q -p no:cacheprovider
	node --test tests/*.test.mjs

verify:
	node scripts/verify_bundle.mjs

docker-up:
	docker compose up --build preview

aws-plan:
	$(PY) scripts/aws_deploy.py plan
