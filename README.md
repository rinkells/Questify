# Questify

Questify is a gamified productivity REST API: quests, character XP and levels,
stats, streaks, achievements, and a skill tree. Built with Python 3.12, Django 5.2,
Django REST framework, PostgreSQL 15+, and Redis.

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
  quests) and capped per 24 hours. Boss rewards are the one exception (see Bosses).
- Completing every active daily and habit quest of a day pays a daily bonus of 25 XP
  and as many coins, once per day in the character's timezone. It needs at least 3
  such quests. A quest whose deadline is at or before the start of the day (local
  midnight) does not count. A deadline missed later in the day is a failure today:
  like a quest failed today, it rules the bonus out for that day, even if it passes
  after the other quests were completed. The bonus counts toward the 24-hour XP cap
  and is cut to what the cap leaves. The `complete` response reports it in
  `daily_bonus` (`{"xp": ..., "coins": ...}` or `null`), apart from `xp_gained` and
  `coins_gained`.
- `GET /api/quests/daily-bonus/` shows the day's progress: `date` (local), `total`
  quests in the set, `completed`, `required` (completions the bonus needs: all of
  them, and at least 3), `awarded` and `xp`.
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

## Wallet and rewards

Completing a quest also earns coins: one per XP awarded, so the 24-hour XP cap limits
coins too. Coins are a separate balance (`coins` in `GET /api/character/`); spending
them never lowers XP, levels, achievements or skill progress.

- `/api/rewards/` manages your own rewards (`title`, `description`, `cost` from 1 to
  100,000, `is_active`). `DELETE` archives a reward: it disappears from the API but
  still counts toward the per-user reward limit, and past redemptions keep it.
- `POST /api/rewards/<id>/redeem/` spends `cost` coins and answers 201 with the
  redemption and the remaining `balance`. It answers 400 when there are not enough
  coins, the reward is inactive or the redemption limit for the last 24 hours is
  reached. A reward can be redeemed any number of times.
- `GET /api/wallet/transactions/` lists every change of the balance, newest first:
  `quest_reward`, `daily_bonus`, `boss_reward`, `redemption` (with the reward's title
  at the time) and `initial_grant`, which gave characters that existed before the
  wallet their earned XP as coins once.

## Bosses

A boss is a bigger goal beaten through its steps, which are your one-time quests.
Its HP is the number of steps still to complete; it is never stored, only counted.

- `/api/bosses/` manages your bosses (`title`, `description`). Each boss shows `hp`,
  `max_hp`, its `steps` (`quest`, `title`, `status`, and `live`: whether the step
  counts toward HP and the reward) and `defeated` (`{xp, coins, defeated_at}` or
  `null`). A step's `status` is `open`, `completed`, `failed`, `expired` (its deadline
  has passed) or `inactive` (its quest is deactivated or archived). `DELETE` archives
  a boss and frees its uncompleted steps for another boss; archived bosses still
  count toward the limit of 50, and at most 5 bosses can be in progress at once.
- `POST /api/bosses/<id>/steps/` with `{"quest": <id>}` adds a step: one of your own
  active, unarchived one-time quests with no completion or failure yet and no
  passed deadline, which is not already a step of a boss. A boss has at most 10
  steps. `DELETE /api/bosses/<id>/steps/<quest id>/` removes a step that is not
  completed; a completed one answers 409.
- Completing the last step beats the boss, if at least 3 steps are completed. It pays
  25 XP per completed step, and as many coins, in full. The `complete` response
  reports every step completed in `boss`: `{id, title, hp, max_hp, defeated}`, where
  `defeated` is `{xp, coins}` on the completion that won, apart from `xp_gained` and
  `coins_gained`.
- The boss reward is the one source of XP outside the 24-hour cap: it rewards long
  work, it is bounded from above by quotas (at most 10 steps × 25 XP per boss, set by
  `QUESTIFY_MAX_BOSS_STEPS` and `QUESTIFY_BOSS_XP_PER_STEP`), and the steps fall under
  the cap themselves. When the cap is spent, completing the last step is refused like
  any other quest, and the boss waits until the cap allows it.
- An archived step, or one whose quest became daily or habit, no longer counts toward
  HP or the reward. A failed or expired step does no damage and blocks the boss until
  you remove it or archive its quest; an inactive one blocks it until its quest is
  active again. Archiving or removing the last open step does not win the fight: add
  a new step and complete it.
- A defeated boss is frozen as it was beaten: `hp` stays 0 and `max_hp` the steps
  that beat it, which are the only `live` ones, whatever happens to their quests
  later. Its other steps no longer hit it, and steps can no longer be added or removed
  (409), but the boss can be renamed or archived.
- The `bosses_defeated_count` achievement condition counts defeated bosses. Create an
  example one ("Boss Slayer") with
  `docker compose exec web python manage.py seed_achievements` (safe to run again).

## Tests and checks

```sh
docker compose exec web python -m pytest -p no:cacheprovider
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
```

To apply new migrations while the stack is running:
`docker compose exec web python manage.py migrate`.
