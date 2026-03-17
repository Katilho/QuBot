.PHONY: help setup run docker-up docker-down update logs clean

help:
	@echo "Available targets:"
	@echo "  make setup      - Install dependencies with uv"
	@echo "  make run        - Run bot locally"
	@echo "  make docker-up  - Build and start with Docker Compose"
	@echo "  make docker-down - Stop Docker Compose services"
	@echo "  make update     - Rebuild the Docker image and restart services"
	@echo "  make logs       - Follow Docker Compose logs"
	@echo "  make clean      - Remove local runtime files"

setup:
	uv sync --locked

run:
	uv run main.py

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

update:
	docker compose up --build -d --force-recreate

logs:
	docker compose logs -f

clean:
	rm -f discord.log
