from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import Character
from quests.models import MAX_XP_REWARD, Quest, QuestCompletion

from .models import Boss, BossDefeat, BossStep


class BossStepError(ValueError):
    """A step the rules refuse (400); the message is shown to the client."""


class BossConflictError(ValueError):
    """A change a defeated boss or a completed step refuses (409)."""


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
                'This boss has been defeated; its steps cannot change.'
            )
        if quest is None or quest.archived_at is not None:
            raise BossStepError('Quest not found.')
        if quest.quest_type != Quest.QuestType.ONE_TIME:
            raise BossStepError('Only one-time quests can be boss steps.')
        if not quest.is_active:
            raise BossStepError('This quest is inactive.')
        if quest.deadline is not None and quest.deadline < now:
            raise BossStepError('This quest deadline has passed.')
        # A failed quest is refused too: it could never be completed.
        if quest.completions.exists():
            raise BossStepError(
                'This quest has already been completed or failed.'
            )
        if BossStep.objects.filter(quest=quest).exists():
            raise BossStepError('This quest is already a step of a boss.')
        if BossStep.objects.filter(boss=boss).count() >= (
            settings.QUESTIFY_MAX_BOSS_STEPS
        ):
            raise BossStepError('This boss already has the most steps allowed.')
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
                'This boss has been defeated; its steps cannot change.'
            )
        if _completed(quest_id).exists():
            raise BossConflictError('A completed step cannot be detached.')
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
