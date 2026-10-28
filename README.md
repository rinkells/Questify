# Questify

Questify is a gamified productivity REST API: quests, character XP and levels,
stats, streaks, achievements, and a skill tree. Built with Python 3.12, Django 5.2, Django REST
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
- Achievements for quests in a category (`quest_count_by_category`) compare the
  category name case-insensitively with the name it had at completion, the same way
  skill requirements do (see below).

## Skill tree

Skills form one catalog shared by all users, edited in the admin. Load an example
tree with:

```sh
docker compose exec web python manage.py seed_skills   # safe to run again
```

- `GET /api/skills/tree/` returns the tree. Each node has `id`, `name`, `parent_id`,
  `status` (`unlocked`, `available` or `locked`) and `requirements`, a list of
  `{type, category, current, required, met}` for showing progress.
- `POST /api/skills/<id>/unlock/` unlocks an `available` skill. It answers 400 when
  requirements are not met (the `detail` lists what is missing), 409 when the parent
  skill is still locked, and 200 if the skill is already unlocked. The 200 response
  lists `achievements_unlocked` (ids earned by this unlock; empty on a repeat).
- A skill that someone has unlocked cannot be deleted in the admin.
- A skill can be unlocked only after its parent. Unlocking has no effect beyond the
  unlock itself, but it can trigger achievements (`skills_unlocked_count`).
- The XP requirement uses the character's total XP, the same value that levels and
  achievements use. Quest-count requirements are computed from the completion
  history, and completions of archived quests still count.

**Category names.** Each user has their own quest categories, so a skill requirement
refers to a category **by name**, compared case-insensitively with the name the
category had when each quest was completed. `Study` and `study` match, but `Studying`
or `Learning` do not. To make progress, use the category names the skill tree uses
(for the example tree: `Study` and `Health`). Renaming a category later does not move
completions recorded under its old name.

## Tests and checks

```sh
docker compose exec web python -m pytest -p no:cacheprovider
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
```

To apply new migrations while the stack is running:
`docker compose exec web python manage.py migrate`.
