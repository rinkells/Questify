from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.errors import ConflictError, DomainError
from core.models import Character
from quests.models import MAX_XP_REWARD, Quest, QuestCompletion

from .models import Boss, BossDefeat, BossStep


class BossStepError(DomainError):
    """A step the rules refuse (400); the message is shown to the client.

    Codes: ``quest_not_found``, ``quest_not_one_time``, ``quest_inactive``,
    ``quest_deadline_passed``, ``quest_already_done``, ``quest_already_step``
    and ``boss_steps_full``.
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


def _completed(quest_id):
    return QuestCompletion.objects.filter(
        quest_id=quest_id,
        status=QuestCompletion.CompletionStatus.COMPLETED,
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
        if BossDefeat.objects.filter(boss=boss).exists():
            raise BossConflictError(
                'This boss has been defeated; its steps cannot change.',
                code='boss_defeated',
            )
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
    def detach_step(user, boss_id, quest_id):
        """Remove a step whose quest has no completed row.

        A failed or archived step can be detached; that is how a failed step
        stops blocking its boss. A completed one stays, as the boss's reward
        counts it.
        """
        Quest.objects.select_for_update().filter(pk=quest_id, user=user).first()
        Character.locked_for_user_id(user.pk)
        boss = _locked_boss(user, boss_id)
        step = BossStep.objects.filter(boss=boss, quest_id=quest_id).first()
        if step is None:
            raise BossStep.DoesNotExist
        if BossDefeat.objects.filter(boss=boss).exists():
            raise BossConflictError(
                'This boss has been defeated; its steps cannot change.',
                code='boss_defeated',
            )
        if _completed(quest_id).exists():
            raise BossConflictError(
                'A completed step cannot be detached.',
                code='step_completed',
            )
        step.delete()

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
