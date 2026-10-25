# Development

This page covers working on Yamtrack from source.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Node.js](https://nodejs.org/) (for the Tailwind CSS build)
- Docker
- Redis

## General setup

### Clone the repository

```bash
git clone https://github.com/FuzzyGrim/Yamtrack.git
cd Yamtrack
```

### Start Redis

If you do not already have Redis running locally, start it with Docker:

```bash
docker run -d --name redis -p 6379:6379 --restart unless-stopped redis:8-alpine
```

### Install dependencies

uv manages the Python environment and dependencies:

```bash
uv sync
uv run pre-commit install
```

Installing the development dependencies includes pre-commit. After `uv run pre-commit install`, the hooks run automatically before each commit. You can also run the full hook set manually:

```bash
uv run pre-commit run --all-files
```

### Configure environment values

Create a `.env` file in the repository root:

```bash
TMDB_API=API_KEY
MAL_API=API_KEY
IGDB_ID=IGDB_ID
IGDB_SECRET=IGDB_SECRET
STEAM_API_KEY=STEAM_API_SECRET
BGG_API_TOKEN=BGG_API_TOKEN
SECRET=SECRET
DEBUG=True
```

See [Environment Variables](env-variables.md) for the full list of supported settings.

### Prepare the database

```bash
cd src
uv run manage.py migrate
```

### Run the app

The repository ships a `Procfile` that defines the web server, the Celery
worker, and the Tailwind watcher. Run all three at once with
[honcho](https://github.com/nickstenning/honcho):

```bash
uvx honcho -e /dev/null start
```

The `-e /dev/null` flag stops honcho from loading the `.env` file into the
process environment; Yamtrack reads `.env` itself, so honcho should leave it
alone.

Alternatively, run each process in its own terminal.

Run the Django development server:

```bash
cd src
uv run manage.py runserver
```

Run the Celery worker with the scheduler in another terminal:

```bash
cd src
uv run celery -A config worker --beat --scheduler django --loglevel DEBUG
```

Run Tailwind in another terminal from the repository root:

```bash
npm ci
npm run watch:css
```

The compiled `src/static/css/main.css` is committed. Run `npm run build:css` and commit the result whenever templates or `input.css` change; CI fails if it is out of date.

Open the development server at:

```text
http://localhost:8000
```

## Documentation

Install the docs dependency group, then serve the docs from the current checkout:

```bash
uv sync --group docs
uv run zensical serve
```

## Testing

Install Playwright browsers before running integration tests:

```bash
uv run playwright install
```

Run the Django test suite from the `src` directory:

```bash
cd src
uv run manage.py test --parallel
```

To run tests for a specific app or test module, pass the test label after `test`:

```bash
cd src
uv run manage.py test app.tests --parallel
```
