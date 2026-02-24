COMPOSE := docker compose -f docker-compose.dev.yml

.PHONY: test test-unit test-integration test-file shell build clean

## Run full test suite
test: build
	$(COMPOSE) run --rm test

## Run unit tests only
test-unit: build
	$(COMPOSE) run --rm test pytest tests/unit/

## Run integration tests only
test-integration: build
	$(COMPOSE) run --rm test pytest tests/integration/

## Run a specific test file (usage: make test-file FILE=tests/unit/test_skill_manager.py)
test-file: build
	$(COMPOSE) run --rm test pytest $(FILE)

## Open an interactive shell in the dev container
shell: build
	$(COMPOSE) run --rm shell

## Build the dev image
build:
	$(COMPOSE) build

## Remove dev containers and images
clean:
	$(COMPOSE) down --rmi local --remove-orphans
