# RCA Copilot developer targets.
#
#   make eval-up     start the Elasticsearch + MongoDB containers the evaluation needs
#   make eval        run the full evaluation (10 scenarios x RUNS, SCALE events each) and
#                    write docs/evaluation.md and docs/evaluation.json
#   make eval-quick  2 scenarios, 2 runs, 20k events; writes to build/eval-quick, not docs/
#   make test        backend unit tests (no services needed)
#
# Needs Ollama running with the model from OLLAMA_MODEL pulled.
# If port 27017 is already taken on your machine, run a spare Mongo and point MONGODB_URI at it:
#   docker run -d --name rca_eval_mongo -p 27018:27017 mongo:7
#   make eval MONGODB_URI=mongodb://localhost:27018

PYTHON      ?= python
RUNS        ?= 5
SCALE       ?= 100000
MONGODB_URI ?= mongodb://localhost:27017
ES_URL      ?= http://localhost:9200

export MONGODB_URI
export ES_URL

.PHONY: eval eval-up eval-quick test

eval-up:
	docker compose up -d elasticsearch mongodb

eval:
	$(PYTHON) scripts/evaluate.py --runs $(RUNS) --scale $(SCALE)

eval-quick:
	$(PYTHON) scripts/evaluate.py --only bad_deploy_pool red_herring_cache --runs 2 --scale 20000 --out-dir build/eval-quick

test:
	cd backend && $(PYTHON) -m pytest tests -q
