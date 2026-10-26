# Questify

Questify is a gamified productivity REST API: quests, character XP and levels,
stats, streaks, and achievements. Built with Python 3.12, Django 5.2, Django REST
framework, PostgreSQL 15+, and Redis.

## Quick start

Requires Docker with the Compose plugin.

```sh
cp .env.example .env        # PowerShell: Copy-Item .env.example .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
```

`.env.example` defaults to `DEBUG=False`, and the dev override turns debug on for the
containers it starts. Running plain `docker compose up` with that file fails on
purpose until `SECRET_KEY`, `ADMIN_ALLOWED_IPS`, `HEALTHCHECK_TOKEN` and
`DRF_NUM_PROXIES` hold real production values. Set `DRF_NUM_PROXIES` to the number of
trusted reverse proxies (1 for `deploy/nginx-questify.conf.example`); otherwise every
client behind the proxy shares one login/register rate-limit bucket.

The API is at `http://localhost:8000/api/` and the interactive docs at
`http://localhost:8000/api/docs/`. The dev override mounts the source tree; restart
the web container (`docker compose restart web`) after code changes.

Useful commands:

```sh
docker compose logs -f web                              # follow logs
docker compose exec web python manage.py createsuperuser # admin at /admin/
docker compose down                                      # stop (keeps data)
docker compose down --volumes                            # stop and delete the database
```

## Try the API

```sh
# register, then log in to get a token
curl -X POST http://localhost:8000/api/auth/register/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"player1","password":"Use-a-strong-password-123!"}'

curl -X POST http://localhost:8000/api/auth/login/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"player1","password":"Use-a-strong-password-123!"}'

# use the token
curl http://localhost:8000/api/quests/ -H 'Authorization: Token YOUR_TOKEN'
```

Tokens expire after 30 days, and logging in again replaces the previous token.

## How it works

- Completing a quest (`POST /api/quests/<id>/complete/`) awards XP, updates stats and
  the daily streak, and can level the character up and unlock achievements.
- XP rewards are set by the server (25 for daily and habit quests, 50 for one-time
  quests) and capped per 24 hours.
- `DELETE` on a quest archives it: it disappears from the API but still counts toward
  the per-user quest limit.

## Tests and checks

```sh
docker compose exec web python -m pytest -p no:cacheprovider
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
```

To apply new migrations while the stack is running:
`docker compose exec web python manage.py migrate`.
