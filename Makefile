COMPOSE ?= docker compose
DEV_IMAGE ?= tam-development:14.8.0
BROWSER_IMAGE ?= tam-browser-check:14.8.0
SERVICE ?= mcp
RERANK_IMAGE ?= tam-rerank-check:14.8.0
TORCH_INDEX_URL ?=

.PHONY: help up down restart logs shell test test-browser browser-image lint build dev-image rerank-image clean ps sql-corpus
help:
	@echo 'up down restart logs shell ps | dev-image test lint build clean sql-corpus | browser-image test-browser'
up:
	$(COMPOSE) up -d
down:
	$(COMPOSE) down
restart:
	$(COMPOSE) restart
logs:
	$(COMPOSE) logs -f
shell:
	$(COMPOSE) exec $(SERVICE) sh
ps:
	$(COMPOSE) ps
dev-image:
	docker build -f docker/Dockerfile.dev -t $(DEV_IMAGE) .
rerank-image:
	docker build -f docker/Dockerfile.rerank --build-arg DEV_IMAGE=$(DEV_IMAGE) --build-arg TORCH_INDEX_URL=$(TORCH_INDEX_URL) -t $(RERANK_IMAGE) .
browser-image:
	docker build -f docker/Dockerfile.browser --build-arg DEVELOPMENT_IMAGE=$(DEV_IMAGE) -t $(BROWSER_IMAGE) .
test-browser:
	docker run --rm --init --shm-size=1g -v "$(CURDIR):/workspace" -w /workspace $(BROWSER_IMAGE) sh -c 'python -m pip install --no-deps -e . && python -m pytest tests/browser -q'
test:
	docker run --rm -v "$(CURDIR):/workspace" -w /workspace -e TAM_MEMORY_DIR=/tmp/tam-tests -e MCP_TRANSPORT=stdio $(DEV_IMAGE) python -m pytest tests -q
sql-corpus:
	docker run --rm -v "$(CURDIR):/workspace" -w /workspace -e TAM_MEMORY_DIR=/tmp/tam-tests -e MCP_TRANSPORT=stdio $(DEV_IMAGE) python scripts/pg_sql_corpus.py
lint:
	docker run --rm -v "$(CURDIR):/workspace" -w /workspace $(DEV_IMAGE) python -m ruff check src tests
build:
	docker run --rm -v "$(CURDIR):/workspace" -w /workspace $(DEV_IMAGE) python -m build
clean:
	$(COMPOSE) down --remove-orphans
