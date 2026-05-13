.PHONY: init up down restart logs build check

init:
	@test -f .env || cp .env.example .env
	@test -f servers.json || cp servers.example.json servers.json
	@mkdir -p keys media tmp
	@echo "Initialized. Edit .env, then run: make up"

build:
	docker compose -f docker-compose.example.yml build

up:
	docker compose -f docker-compose.example.yml up -d --build

down:
	docker compose -f docker-compose.example.yml down

restart:
	docker compose -f docker-compose.example.yml restart

logs:
	docker compose -f docker-compose.example.yml logs -f --tail=200

check:
	python3 -m py_compile auth.py jiaoops.py telegram-bot/bot.py optional/update_from_nezha.py
	@! grep -R "BOT_TOKEN=.*[0-9][0-9][0-9].*:" -n . --exclude='.env.example' --exclude-dir='.git' || (echo "Possible token leak" && exit 1)
	@! grep -R "BEGIN .*PRIVATE KEY" -n . --exclude-dir='.git' || (echo "Private key leak" && exit 1)
