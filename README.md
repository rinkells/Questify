# Questify

Questify is a gamified productivity REST API with a server-rendered web UI: quests,
character XP and levels, stats, streaks, achievements, and a skill tree. Built with
Python 3.12, Django 5.2, Django REST framework, htmx, PostgreSQL 15+, and Redis.

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

## Web UI

The interface is at `http://localhost:8000/`, in Ukrainian, with its pages at the URL
root: `/login/`, `/register/`, `POST /logout/`, `/today/`, where `/` leads, `/quests/`,
`/bosses/`, `/skills/`, `/achievements/` and `/rewards/`.

- **Today.** The header shows the coin balance next to the avatar. The character card
  holds the level, the XP bar and up to five stat tiles. Below it come the daily bonus,
  as a thin strip, and the quests to act on. The side column is three cards: the streak
  with its freezes, the 24-hour XP limit and the next achievement. All daily and habit
  quests are listed; one-time quests are the five nearest open ones, with a link to all
  quests when there are more. Quests finished today stay on the screen, muted.
- **All quests.** `/quests/` lists everything, newest first, with "show more" for the
  next 20. Filters by state, type and category switch without a reload and stay in the
  address (`/quests/?category=3&type=daily`), so a link can be copied and a page
  reloaded. Active quests are the daily and habit ones and one-time quests with no
  completion or failure; completed quests are those completed at least once and one-time
  quests that failed; the archive holds what was archived. A quest switched off in its
  form stays among the active ones, so it can be switched back on. "New quest", on this
  screen and on Today, opens one form at `/quests/new/`. It takes the title, a category
  (an existing one or a new name, which reuses a category that differs only in case,
  spaces or apostrophe), the type and an optional deadline in your timezone. The reward is read out next
  to the type: the server sets it. Rows can be completed, failed, edited or sent to the
  archive. Archiving cannot be undone: neither the API nor the services can restore a
  quest, and archived quests still count toward the quota.
- **Bosses.** `/bosses/` lists your bosses, those in progress first and the defeated
  after them. A card shows the HP bar (`HP 3 з 5`) and each step with its state: open,
  done, failed, deadline passed or switched off. It says what stands in the way: a
  failed or expired step blocks the win until it is detached, a switched-off one until
  its quest is active again, and with fewer than three steps that count the card says
  how many more to add (`Додай ще 2 кроки, щоб бос міг бути переможений`). "New boss"
  opens a form at `/bosses/new/` that takes a title, a description, any of your free
  one-time quests and new steps, one title per line, all or nothing: if a quest was
  taken or finished meanwhile, or a quota runs out, no boss and no quest is created and
  the form says why. Steps can be added on the card, either a free quest from the list
  or a new one-time quest (a title and, if you like, a category and a deadline), and
  detached there, except that a completed step stays, a defeated boss is frozen, and a
  step that counts stays when fewer than three such steps would be left (a boss with
  exactly three and a failed step needs another step first; a step that does not count
  can always go).
  Archiving releases
  the unfinished steps and cannot be undone, and archived bosses still count toward the
  quota. Steps are completed on Today and All quests, which show `Бос: −1 HP` or, for
  the last one, `Боса переможено` with what it paid.
- **Skills.** `/skills/` shows the tree as nested lists. Each node has its status (open,
  available, locked) and, until it is open, its requirements with progress
  (`квестів у «Навчання»: 17 з 20`). Only an available skill has a button. A refusal
  appears next to the node, which can only happen when the page is out of date: 400 if
  the requirements are no longer met, 409 if the parent is locked. Pressing the button
  on a skill that is already open is not an error.
- **Achievements.** `/achievements/` is a grid of cards under a count and a bar
  (`Відкрито 2 з 5`): earned ones with their date, visible ones with their description,
  hidden ones as `???` until earned. There is no progress per achievement, because the
  conditions only answer yes or no.
- **Rewards.** `/rewards/` shows the coin balance, your own rewards and the coin history,
  newest first, with "show more" for the next 20. A reward you cannot afford has its
  button disabled and says how many coins are missing. The button asks for confirmation
  first, naming the reward and its cost, and spends nothing until confirmed; the balance
  in the header and the history then change without a reload. The confirmation's form
  holds a one-time token kept in the session, so a double click or a repeated request
  spends the coins once and answers `Вже оброблено`. A refusal (not enough coins, with
  the balance and the cost; the limit for the last 24 hours; a reward switched off)
  appears on the card. "New reward" opens a form at `/rewards/new/`, which also edits a
  reward and switches it off. Archiving cannot be undone, and archived rewards still
  count toward the quota; what was redeemed stays in the history.
- **Before working with these screens**, load the catalogs they read:

  ```sh
  docker compose exec web python manage.py seed_skills
  docker compose exec web python manage.py seed_achievements
  ```

  Both are safe to run again, and running them again brings the names and descriptions
  of their rows up to date, in place. Rows created by an earlier version under English
  names are renamed, so nobody loses an unlock, and requirements in the categories
  `Study` and `Health` move to `Навчання` and `Здоров'я`. Completions keep the category
  name they were recorded with, so those made under the old names stop counting toward
  the requirements (see Category names below). Conditions, thresholds, parents and order
  are left as they are, and a requirement removed in the admin does not come back.

  A fresh database has neither stat types nor achievements: the card then shows no
  tiles, not zeros, and the third side card says there is nothing to earn. The tiles are
  the first five `StatType` rows by id. `seed_skills` creates three (`WIS`, `INT`, `STR`),
  so the card shows three until more are added in the admin. The database and the API keep
  the stat types' codes and English names; the web shows these three in Ukrainian through
  `STAT_LABELS` in `web/texts.py`, keyed by `StatType.name`. Any other type shows its
  `display_name`. Seeded skills and achievements are named in Ukrainian in the database,
  so the API returns those names too.
- **Completing and failing.** Both work through htmx, which updates the row and the rest
  of the screen in place and shows what was earned in toasts. A refusal (a passed
  deadline, the XP limit, a quest already done) appears next to the row. Without
  JavaScript the same forms post normally and redirect back. They spend the same `user`
  budget as the API; reading pages does not.

- **Sign-in.** The forms use the same checks and the same throttle counters as
  `/api/auth/login/` and `/api/auth/register/`, so a client has one budget whichever
  door it uses. Registering logs in and takes the timezone from the browser (UTC if it
  is not a valid zone).
- **Sessions and tokens are independent.** A web login does not revoke API tokens, an
  API login does not end web sessions, and an account may hold several web sessions.
- **Session length.** A session lasts a fixed 30 days from login. Expired rows stay in
  the database until they are cleared, so run this from cron on the host:

  ```cron
  0 4 * * * cd /path/to/Questify && docker compose exec -T web python manage.py clearsessions
  ```

- **Behind a proxy.** `CSRF_TRUSTED_ORIGINS` is only needed when the proxy changes the
  `Host` header. In production it takes exact `https://` origins, without wildcards.

### Static files and fonts

WhiteNoise serves static files from `STATIC_ROOT` (no separate web server is needed),
and the container runs `collectstatic` on every start. With `DEBUG=True` they come
straight from the source tree.

Django caches templates even with `DEBUG=True`, and `gunicorn --reload` watches only
Python files. After editing a template, restart the container
(`docker compose restart web`) to see the change; CSS and JavaScript need no restart.

Everything is local; no page loads anything from another site:

- `web/static/web/fonts/`: [Onest](https://github.com/googlefonts/onest) and
  [Unbounded](https://github.com/googlefonts/unbounded), Cyrillic and Latin subsets,
  as WOFF2 files from [Google Fonts](https://fonts.google.com/). Both are under the SIL
  Open Font License (`OFL-Onest.txt`, `OFL-Unbounded.txt` next to them).
- `web/static/web/vendor/htmx/`: htmx, unmodified. Its `README.md` has the version, the
  source and the SHA-256, which a test checks.

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
  category name with the name it had at completion, ignoring case and apostrophes, the
  same way skill requirements do (see below).
- Category names are unique per account, compared the same way. `POST` or `PATCH` on
  `/api/quest-categories/` with a name you already have answers 400:
  `{"name": "You already have a category with this name."}`. A category may change the
  case or apostrophe of its own name.

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
refers to a category **by name**, compared with the name the category had when each
quest was completed. Case and spaces around the name are ignored, and so is which
apostrophe was typed (`'`, `’` or `ʼ`): `Навчання` and `навчання` match, as do `Здоров'я`
and `Здоров’я`, but `Study` or `Навчальне` do not. To make progress, use the category
names the skill tree uses (for the example tree: `Навчання` and `Здоров'я`). Renaming a
category later does not move completions recorded under its old name.

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
  completed; a completed one answers 409. A step that counts toward HP is refused with
  400 when fewer than 3 such steps would be left, with the detail
  `A boss must keep at least 3 steps.` Creating a boss with fewer steps, and adding them
  later, stays allowed.
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
  example one ("Переможець босів") with
  `docker compose exec web python manage.py seed_achievements` (safe to run again).

## Monthly summary

`GET /api/summary/monthly/?month=YYYY-MM` sums up one calendar month of your own
history in your character's timezone: from local midnight on the 1st to local midnight
on the 1st of the next month (`starts_at` and `ends_at`, in your offset). Without
`month` it shows the current month so far. Nothing is stored: the summary is computed
on each request, so a past month changes if its history does (an admin removing an
unlocked achievement, for example).

- `quests_completed` and `quests_failed`; `estimated_completions` counts completions
  from before XP was recorded, whose XP was only estimated.
- `xp` (`{total, quests, daily_bonus, bosses}`), `daily_bonuses`, `coins_earned`,
  `coins_spent` and `balance_end` (the balance at the end of the month, or now) come
  from the wallet, which pays one coin per XP, so they are exact and `coins_earned`
  equals `xp.total`. The `initial_grant` counts only in the balance.
- `bosses_defeated`, `achievements_unlocked` and `skills_unlocked` (`{count, names}`,
  in the order they were unlocked), and `current_level`: the level now, since levels
  have no history.
- Summaries start with your wallet history and go back at most 24 months. For a
  character that existed before the wallet, the history starts with its
  `initial_grant`, and that month is `partial: true`: XP and coins earned in it before
  the grant are missing. For everyone else it starts with the account. A future, older
  or malformed month answers 400.
- The endpoint has a throttle of its own (120 requests per hour per user) on top of
  the general one.

## Tests and checks

```sh
docker compose exec web python -m pytest -p no:cacheprovider
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
```

To apply new migrations while the stack is running:
`docker compose exec web python manage.py migrate`.
