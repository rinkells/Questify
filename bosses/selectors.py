"""Reads of a user's bosses and the quests that could become their steps."""
from django.db.models import Prefetch, Q
from django.utils import timezone

from quests.models import Quest

from .models import Boss, BossStep


def bosses_for_display(queryset):
    """What BossSerializer reads: progress, steps and the defeat.

    Annotated, so never locked through it (see ``Boss.objects.with_progress``).
    """
    return queryset.with_progress().prefetch_related(
        Prefetch(
            'steps',
            queryset=BossStep.objects.with_status().select_related(
                'quest',
            ).order_by('pk'),
        ),
        'defeat',
    )


def bosses_for_user(user):
    """The user's unarchived bosses, without the display annotations."""
    return Boss.objects.filter(
        user=user,
        archived_at__isnull=True,
    ).order_by('pk')


def list_bosses(user):
    """The user's unarchived bosses with progress, steps and defeat."""
    return bosses_for_display(bosses_for_user(user))


def get_boss(user, boss_id):
    """One unarchived boss of the user's for display; ``Boss.DoesNotExist``."""
    return list_bosses(user).get(pk=boss_id)


def displayed_boss(boss_id):
    """A boss by id for display, once the caller has checked it is theirs."""
    return bosses_for_display(Boss.objects.all()).get(pk=boss_id)


def step_status(step, now=None):
    """Where a step stands: 'completed', 'failed', 'expired', 'inactive' or 'open'.

    ``step`` comes from ``BossStep.objects.with_status()``. 'expired' is a
    deadline that has passed, 'inactive' a deactivated or archived quest. A
    live step that is failed or expired blocks the boss until detached or
    archived; an inactive one, until its quest is active again.
    """
    if step.completed:
        return 'completed'
    if step.failed:
        return 'failed'
    # The rules complete_quest applies.
    quest = step.quest
    if quest.deadline is not None and quest.deadline < (now or timezone.now()):
        return 'expired'
    if not quest.is_active:
        return 'inactive'
    return 'open'


def attachable_quests(user, *, now=None):
    """The quests ``BossService.attach_step`` would accept as a step.

    The user's own, active, unarchived, one-time, with no completion or
    failure and no passed deadline, and not a step of any boss yet.
    """
    now = now or timezone.now()
    return Quest.objects.filter(
        user=user,
        is_active=True,
        archived_at__isnull=True,
        quest_type=Quest.QuestType.ONE_TIME,
        boss_step__isnull=True,
        completions__isnull=True,
    ).filter(
        Q(deadline__isnull=True) | Q(deadline__gte=now),
    ).order_by('pk')
