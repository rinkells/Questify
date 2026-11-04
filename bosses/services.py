from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.errors import ConflictError, DomainError
from core.models import Character
from quests.models import MAX_XP_REWARD, Quest, QuestCompletion
from quests.services import QuestService

from .models import Boss, BossDefeat, BossStep, live_steps


class BossStepError(DomainError):
    """A step the rules refuse (400); the message is shown to the client.

    Codes: ``quest_not_found``, ``quest_not_one_time``, ``quest_inactive``,
    ``quest_deadline_passed``, ``quest_already_done``, ``quest_already_step``,
    ``boss_steps_full`` and ``boss_min_steps`` (param ``minimum``).
    """

    code = 'step_refused'


class BossConflictError(ConflictError):
    """A change a defeated boss or a completed step refuses (409).

    Codes: ``boss_defeated`` and ``step_completed``.
    """


class BossLimitError(DomainError):
    """A new boss beyond the user's quotas (400).

    Codes: ``boss_limit_reached`` and ``active_boss_limit_reached``.
    """


BOSS_FIELDS = frozenset(('title', 'description'))


def _check_fields(fields):
    unknown = set(fields) - BOSS_FIELDS
    if unknown:
        raise TypeError(f'Unexpected fields: {sorted(unknown)}')


def boss_reward(completed_steps):
    """The XP a beaten boss pays in full: a share per completed live step.

    Positive, since a defeat needs QUESTIFY_BOSS_MIN_STEPS completed steps
    and settings refuse a zero share or minimum. Not cut by the 24-hour cap.
    """
    return min(
        settings.QUESTIFY_BOSS_XP_PER_STEP * completed_steps,
        MAX_XP_REWARD,
    )


def _locked_boss(user, boss_id):
    boss = Boss.objects.select_for_update().filter(
        pk=boss_id,
        user=user,
        archived_at__isnull=True,
    ).first()
    if boss is None:
        raise Boss.DoesNotExist
    return boss


def _refuse_if_defeated(boss):
    if BossDefeat.objects.filter(boss=boss).exists():
        raise BossConflictError(
            'This boss has been defeated; its steps cannot change.',
            code='boss_defeated',
        )


def _completed(quest_id):
    return QuestCompletion.objects.filter(
        quest_id=quest_id,
        status=QuestCompletion.CompletionStatus.COMPLETED,
    )


def _check_live_steps_stay(boss, step):
    """Refuse to detach a step that counts if too few such steps would be left.

    A step that does not count changes nothing, so it can always go, also from a
    boss already below the minimum. The steps are read under the character lock the
    caller holds, so parallel detachings see each other's result.
    """
    live = set(
        BossStep.objects.filter(live_steps(), boss=boss).values_list('pk', flat=True),
    )
    minimum = settings.QUESTIFY_BOSS_MIN_STEPS
    if step.pk in live and len(live) - 1 < minimum:
        raise BossStepError(
            f'A boss must keep at least {minimum} steps.',
            code='boss_min_steps',
            minimum=minimum,
        )


class BossService:
    # Every write locks the quest (if any), then the character, then the
    # boss. complete_quest takes the quest before the character and never
    # locks a boss, so this order cannot deadlock with it; the character
    # lock serializes these writes with the user's completions, which is
    # what BossProgressHandler relies on to read steps without locks.

    @staticmethod
    @transaction.atomic
    def attach_step(user, boss_id, quest_id):
        """Make one of the user's quests a step of the boss.

        The quest lock also waits for an archive or a type change of the
        quest in progress, which take only that row.
        """
        quest = Quest.objects.select_for_update().filter(
            pk=quest_id,
            user=user,
        ).first()
        Character.locked_for_user_id(user.pk)
        # After the lock, as in complete_quest.
        now = timezone.now()
        boss = _locked_boss(user, boss_id)
        _refuse_if_defeated(boss)
        if quest is None or quest.archived_at is not None:
            raise BossStepError('Quest not found.', code='quest_not_found')
        if quest.quest_type != Quest.QuestType.ONE_TIME:
            raise BossStepError(
                'Only one-time quests can be boss steps.',
                code='quest_not_one_time',
            )
        if not quest.is_active:
            raise BossStepError(
                'This quest is inactive.',
                code='quest_inactive',
            )
        if quest.deadline is not None and quest.deadline < now:
            raise BossStepError(
                'This quest deadline has passed.',
                code='quest_deadline_passed',
            )
        # A failed quest is refused too: it could never be completed.
        if quest.completions.exists():
            raise BossStepError(
                'This quest has already been completed or failed.',
                code='quest_already_done',
            )
        if BossStep.objects.filter(quest=quest).exists():
            raise BossStepError(
                'This quest is already a step of a boss.',
                code='quest_already_step',
            )
        if BossStep.objects.filter(boss=boss).count() >= (
            settings.QUESTIFY_MAX_BOSS_STEPS
        ):
            raise BossStepError(
                'This boss already has the most steps allowed.',
                code='boss_steps_full',
                limit=settings.QUESTIFY_MAX_BOSS_STEPS,
            )
        return BossStep.objects.create(boss=boss, quest=quest, added_at=now)

    @staticmethod
    @transaction.atomic
    def add_new_step(user, boss_id, quest_data):
        """Create a one-time quest and make it a step of the boss, all or nothing.

        ``quest_data`` is what ``QuestService.create_quest`` takes (validated
        fields and ``new_category``), whatever type it names: a step is one-time.
        A quota or a rule that refuses takes the quest and any category it
        brought back with it.

        There is no quest to lock before the character: quest quotas are checked
        under that lock, and the new row is out of everyone's reach until the
        commit. The boss comes before the quest, so a foreign, archived or defeated
        boss answers as such also for a user out of quests.
        """
        Character.locked_for_user_id(user.pk)
        _refuse_if_defeated(_locked_boss(user, boss_id))
        quest = QuestService.create_quest(
            user,
            **{**quest_data, 'quest_type': Quest.QuestType.ONE_TIME},
        )
        return BossService.attach_step(user, boss_id, quest.pk)

    @staticmethod
    @transaction.atomic
    def detach_step(user, boss_id, quest_id):
        """Remove a step whose quest has no completed row.

        A failed or archived step can be detached; that is how a failed step
        stops blocking its boss. A completed one stays, as the boss's reward
        counts it, and so does a step that counts when fewer than
        QUESTIFY_BOSS_MIN_STEPS such steps would be left (``boss_min_steps``).
        Returns the quest, which is a step no longer.
        """
        quest = Quest.objects.select_for_update().filter(
            pk=quest_id,
            user=user,
        ).first()
        Character.locked_for_user_id(user.pk)
        boss = _locked_boss(user, boss_id)
        step = BossStep.objects.filter(boss=boss, quest_id=quest_id).first()
        if step is None:
            raise BossStep.DoesNotExist
        _refuse_if_defeated(boss)
        if _completed(quest_id).exists():
            raise BossConflictError(
                'A completed step cannot be detached.',
                code='step_completed',
            )
        _check_live_steps_stay(boss, step)
        step.delete()
        return quest

    @staticmethod
    @transaction.atomic
    def archive_boss(user, boss_id):
        """Archive the boss and release its unfinished steps.

        Released quests can join another boss; completed steps stay as the
        record of what was done. No quest is locked: whether a step is
        completed changes only under the character lock.
        """
        Character.locked_for_user_id(user.pk)
        now = timezone.now()
        boss = _locked_boss(user, boss_id)
        BossStep.objects.filter(boss=boss).exclude(
            quest__completions__status=QuestCompletion.CompletionStatus.COMPLETED,
        ).delete()
        boss.archived_at = now
        boss.save(update_fields=('archived_at', 'updated_at'))
        return boss

    @staticmethod
    @transaction.atomic
    def create_boss(user, **fields):
        """Create a boss within the user's quotas; returns the plain row.

        Archived bosses count toward ``QUESTIFY_MAX_BOSSES_PER_USER``, as
        archived quests do; ``QUESTIFY_MAX_ACTIVE_BOSSES`` counts the
        unarchived, undefeated ones. Show it with ``selectors.displayed_boss``.
        """
        _check_fields(fields)
        Character.locked_for_user_id(user.pk)
        bosses = Boss.objects.filter(user=user)
        if bosses.count() >= settings.QUESTIFY_MAX_BOSSES_PER_USER:
            raise BossLimitError(
                'Boss limit reached for this account.',
                code='boss_limit_reached',
            )
        if bosses.filter(
            archived_at__isnull=True,
            defeat__isnull=True,
        ).count() >= settings.QUESTIFY_MAX_ACTIVE_BOSSES:
            raise BossLimitError(
                'Too many bosses in progress; defeat or archive one first.',
                code='active_boss_limit_reached',
            )
        return Boss.objects.create(user=user, **fields)

    @staticmethod
    @transaction.atomic
    def create_boss_with_steps(user, *, quest_ids=(), new_steps=(), **fields):
        """Create a boss with these quests and new steps, all or nothing.

        ``new_steps`` is a ``quest_data`` for ``add_new_step`` per step. A quest
        or new step refused takes the new boss and every quest created for it
        back with it. The quests are locked before ``create_boss`` takes the
        character, in id order: creating first would lock the character before
        the quests, the opposite of complete_quest, and two creations could
        otherwise wait for each other's quests. New steps need no such lock.
        """
        quest_ids = sorted(set(quest_ids))
        if len(quest_ids) + len(new_steps) > settings.QUESTIFY_MAX_BOSS_STEPS:
            raise BossStepError(
                'This boss already has the most steps allowed.',
                code='boss_steps_full',
                limit=settings.QUESTIFY_MAX_BOSS_STEPS,
            )
        list(
            Quest.objects.select_for_update().filter(
                pk__in=quest_ids,
                user=user,
            ).order_by('pk').values_list('pk', flat=True)
        )
        boss = BossService.create_boss(user, **fields)
        for quest_id in quest_ids:
            BossService.attach_step(user, boss.pk, quest_id)
        for quest_data in new_steps:
            BossService.add_new_step(user, boss.pk, quest_data)
        return boss

    @staticmethod
    @transaction.atomic
    def update_boss(user, boss_id, **fields):
        """Change title or description, also after a defeat; the steps stay.

        Raises ``Boss.DoesNotExist`` for an archived or foreign boss, which
        covers one archived after the caller looked it up.
        """
        _check_fields(fields)
        try:
            boss = _locked_boss(user, boss_id)
        except (TypeError, ValueError):
            raise Boss.DoesNotExist from None
        for name, value in fields.items():
            setattr(boss, name, value)
        boss.save()
        return boss
