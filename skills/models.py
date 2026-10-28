from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, models, transaction
from django.db.models import F, Q

from core.models import StatType
from quests import selectors


# PostgreSQL table lock modes on the skill catalog, the Skill and
# SkillRequirement tables. Unlocks and revocations take the read mode, which
# does not conflict with itself, so they run in parallel, but every INSERT,
# UPDATE or DELETE on either table (QuerySet.update() included) conflicts
# with it. An unlock therefore never checks a skill or requirements that are
# still being changed, and a write waits for unlocks in progress. Parent edits,
# requirement edits in the admin and deletions take the write mode, which also
# conflicts with itself. A new child takes the insert mode, the lock its
# INSERT takes anyway, early: it waits for a deletion in progress, so the
# parent can be checked after it, and it does not block other inserts.
#
# Both tables are locked in one statement, Skill first, and before any row
# lock (unlock_skill takes the character row after the catalog). Code that
# writes requirements before their skill in one transaction can deadlock with
# an unlock (PostgreSQL then aborts one of them), which is why Skill.delete()
# takes the write lock before its cascade deletes the requirements.
CATALOG_READ_LOCK = 'SHARE'
CATALOG_WRITE_LOCK = 'SHARE ROW EXCLUSIVE'
CATALOG_INSERT_LOCK = 'ROW EXCLUSIVE'
_CATALOG_LOCK_MODES = frozenset((
    CATALOG_READ_LOCK,
    CATALOG_WRITE_LOCK,
    CATALOG_INSERT_LOCK,
))


def lock_skill_catalog(mode):
    """Lock the catalog tables until the end of the transaction."""
    if mode not in _CATALOG_LOCK_MODES:
        # The mode is part of the SQL text, so only known values pass.
        raise ValueError(f'Unknown catalog lock mode: {mode!r}')
    tables = ', '.join(
        connection.ops.quote_name(model._meta.db_table)
        for model in (Skill, SkillRequirement)
    )
    with connection.cursor() as cursor:
        cursor.execute(f'LOCK TABLE {tables} IN {mode} MODE')


class Skill(models.Model):
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True, default='')
    category = models.ForeignKey(
        StatType,
        on_delete=models.PROTECT,
        related_name='skills',
    )
    # PROTECT: deleting a node must not silently remove a subtree that users
    # have already unlocked. Move or delete the children first.
    parent = models.ForeignKey(
        'self',
        on_delete=models.PROTECT,
        related_name='children',
        null=True,
        blank=True,
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('order', 'id')
        constraints = [
            models.CheckConstraint(
                condition=Q(parent__isnull=True) | ~Q(parent=F('id')),
                name='skill_parent_not_self',
            ),
        ]

    def clean(self):
        super().clean()
        if connection.in_atomic_block:
            # The admin validates inside the transaction it then saves in.
            # Locking here keeps the tree fixed until that commit, so a
            # conflicting concurrent edit or deletion shows up as this form
            # error and the re-check in save() cannot fail with a 500.
            self._lock_tree()
        error = self._parent_error()
        if error:
            raise ValidationError({'parent': error})

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if self.parent_id is None or (
            update_fields is not None
            and not {'parent', 'parent_id'} & set(update_fields)
        ):
            # The parent is not written, so the tree cannot change, whatever
            # a stale in-memory parent_id says.
            super().save(*args, **kwargs)
            return
        # clean() may have run without the lock (outside a transaction, or
        # not at all). Checking again under it means two concurrent edits
        # cannot each pass against the old tree and together form a cycle,
        # an unlock cannot slip in between the check and the write, and a
        # parent deleted meanwhile is an error here, not a foreign key
        # violation at commit.
        with transaction.atomic():
            self._lock_tree()
            error = self._parent_error()
            if error:
                raise ValidationError({'parent': error})
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Lock in the order unlocks do before the cascade deletes the
        # requirements, which would otherwise lock their table first.
        with transaction.atomic():
            lock_skill_catalog(CATALOG_WRITE_LOCK)
            return super().delete(*args, **kwargs)

    def _lock_tree(self):
        """Take the catalog lock the parent checks need, if any.

        Moving an existing skill can form a cycle or strand its holders, so
        it takes the write lock. A new skill is nobody's ancestor and nobody
        holds it yet; it only needs its parent to survive until the commit,
        so it takes the insert lock, which waits for a deletion in progress
        but, unlike the write lock, is not exclusive among inserts and so
        does not serialize a long transaction such as seed_skills.
        """
        if self.parent_id is None:
            return
        lock_skill_catalog(
            CATALOG_INSERT_LOCK if self.pk is None else CATALOG_WRITE_LOCK,
        )

    def _parent_error(self):
        if self.parent_id is None:
            return None
        if self.pk is not None:
            old_parent_id = Skill.objects.filter(pk=self.pk).values_list(
                'parent_id',
                flat=True,
            ).first()
            if old_parent_id == self.parent_id:
                return None
        if not Skill.objects.filter(pk=self.parent_id).exists():
            return 'The parent skill was deleted. Choose another parent.'
        if self.pk is None:
            return None
        # The database only rejects a direct self-reference; longer cycles are
        # caught here by walking up from the new parent. The catalog is small
        # and locked, so it is read in one query rather than one per level.
        parents = dict(Skill.objects.values_list('id', 'parent_id'))
        ancestor_id = self.parent_id
        visited = set()
        while ancestor_id is not None and ancestor_id not in visited:
            if ancestor_id == self.pk:
                return 'A skill cannot be its own ancestor.'
            visited.add(ancestor_id)
            ancestor_id = parents.get(ancestor_id)
        # A new parent must not leave users holding this skill without it.
        stranded = UserSkill.objects.filter(skill_id=self.pk).exclude(
            user__user_skills__skill_id=self.parent_id,
        ).count()
        if stranded:
            return (
                f'{stranded} user(s) have this skill unlocked without the new '
                'parent. Choose a parent they all have, or no parent.'
            )
        return None

    def __str__(self):
        return self.name


class SkillRequirement(models.Model):
    class RequirementType(models.TextChoices):
        TOTAL_XP = 'total_xp', 'Total XP'
        QUEST_COUNT = 'quest_count', 'Quest count'
        QUEST_COUNT_BY_CATEGORY = (
            'quest_count_by_category',
            'Quest count by category',
        )

    skill = models.ForeignKey(
        Skill,
        on_delete=models.CASCADE,
        related_name='requirements',
    )
    requirement_type = models.CharField(
        max_length=40,
        choices=RequirementType.choices,
    )
    threshold = models.PositiveIntegerField()
    # Matched case-insensitively against QuestCompletion.category_name_at_completion.
    # Categories belong to individual users, so a catalog-wide skill refers to
    # them by name rather than by foreign key.
    category_name = models.CharField(max_length=100, blank=True, default='')
    # selectors.category_key(category_name), written by clean() and save().
    # Uniqueness is enforced on this stored key rather than on
    # Lower('category_name'), so the database and Python share one definition:
    # PostgreSQL lower() differs from str.lower() on 'İ', and under a C locale
    # on every non-ASCII letter. Lowercasing 'İ' yields two code points, hence
    # the larger max_length.
    category_key = models.CharField(
        max_length=200,
        blank=True,
        default='',
        editable=False,
    )

    class Meta:
        ordering = ('id',)
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(requirement_type='quest_count_by_category')
                    & ~Q(category_name='')
                ) | (
                    ~Q(requirement_type='quest_count_by_category')
                    & Q(category_name='')
                ),
                name='skill_requirement_category_matches_type',
            ),
            # Deferred to commit: the admin saves inline rows one at a time,
            # so swapping two names, or renaming a row to the name of a row
            # deleted in the same save, passes through a temporary duplicate.
            models.UniqueConstraint(
                fields=('skill', 'requirement_type', 'category_key'),
                name='unique_skill_requirement',
                deferrable=models.Deferrable.DEFERRED,
            ),
        ]

    def clean(self):
        super().clean()
        self._normalize_category()
        is_category_type = (
            self.requirement_type
            == self.RequirementType.QUEST_COUNT_BY_CATEGORY
        )
        if is_category_type and not self.category_name:
            raise ValidationError({
                'category_name': 'Required for a quest count by category.',
            })
        if not is_category_type and self.category_name:
            raise ValidationError({
                'category_name': 'Only a quest count by category uses a category.',
            })

    def save(self, *args, **kwargs):
        # Again here for rows saved without clean(): an untrimmed 'Study '
        # would be shown as is and could sit next to 'Study'.
        self._normalize_category()
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'category_name' in update_fields:
            kwargs['update_fields'] = {*update_fields, 'category_key'}
        super().save(*args, **kwargs)

    def _normalize_category(self):
        self.category_name = self.category_name.strip()
        self.category_key = selectors.category_key(self.category_name)

    def __str__(self):
        return f'{self.skill}: {self.get_requirement_type_display()} {self.threshold}'


class UserSkill(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='user_skills',
    )
    # PROTECT, like Skill.parent: deleting a skill must not silently erase
    # every user's unlock of it.
    skill = models.ForeignKey(
        Skill,
        on_delete=models.PROTECT,
        related_name='user_skills',
    )
    unlocked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'skill'),
                name='unique_user_skill',
            ),
        ]

    def __str__(self):
        return f'{self.user}: {self.skill}'
