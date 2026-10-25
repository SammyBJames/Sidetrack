web: uv run python src/manage.py runserver ${YAMTRACK_PORT:-8000}
worker: uv run celery --workdir src -A config worker --beat --loglevel DEBUG --without-mingle --without-gossip
css: npm run watch:css
