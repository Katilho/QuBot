# QuBot

Simple Discord bot with a JSON database.

## Requirements

- Python 3.11+
- `uv` (recommended)
- Docker + Docker Compose (optional)

## Run locally

1. Create your env file:

```bash
cp .env.example .env
```

2. Edit `.env` and set your real token:

```env
DISCORD_TOKEN=your_token_here
```

3. Install dependencies:

```bash
uv sync --locked
```

4. Start the bot:

```bash
uv run main.py
```

## Run with Docker

1. Create env file:

```bash
cp .env.example .env
```

2. Set `DISCORD_TOKEN` in `.env`.

3. Start container:

```bash
docker compose up --build -d
```

4. View logs:

```bash
docker compose logs -f
```

## Notes

- User data is stored in `db.json`.
- Logs are written to `discord.log`.
