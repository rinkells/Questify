from django.db import connections


def refuse_unsafe_rollbacks(sender, *, plan=None, apps=None, using=None, **kwargs):
	"""Run the rollback guards of every migration a rollback reverts, first.

	Each migration is reverted in a transaction of its own, so a guard that
	refuses partway through keeps the steps reverted before it: with daily
	bonuses paid, ``migrate quests 0014`` used to drop wallet 0004's
	boss_defeat column before wallet 0003's guard stopped it, and every coin
	credit failed until the migrations went forward again. pre_migrate
	carries the whole plan before anything changes, so this runs the guards
	listed in each reverted migration's ``rollback_guards`` against the
	current state and stops the rollback while nothing has been reverted.
	The guards stay operations of their migrations too, for a
	MigrationExecutor run directly, which sends no signal.

	A guard only reads, and nothing it reads changes in the earlier steps of
	the same rollback, so its answer up front is its answer at its step. The
	signal does not tell a --fake run apart, so that is refused the same way.
	"""
	if not plan:
		return
	with connections[using].schema_editor(atomic=False) as schema_editor:
		for migration, backwards in plan:
			if not backwards:
				continue
			for guard in getattr(migration, 'rollback_guards', ()):
				guard(apps, schema_editor)
