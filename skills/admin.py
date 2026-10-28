from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import ProtectedError
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponseRedirect
from django.utils.text import capfirst

from core.models import Character
from quests.selectors import category_key

from .models import (
    CATALOG_READ_LOCK,
    CATALOG_WRITE_LOCK,
    Skill,
    SkillRequirement,
    UserSkill,
    lock_skill_catalog,
)


class SkillRequirementFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        # The unique constraint is on category_key, which is not a form field,
        # and it is deferred, so form validation never checks it and a
        # duplicate would only fail at commit with a 500. The check covers
        # the requirements as they will be after the save: the stored rows,
        # including any added since the page was rendered (in another tab or
        # by another admin), with the posted changes applied.
        keys = {}
        if self.instance.pk is not None:
            if connection.in_atomic_block:
                # As the admin saves in this transaction, the write lock keeps
                # a concurrent save from adding a duplicate until the commit.
                lock_skill_catalog(CATALOG_WRITE_LOCK)
            keys = {
                pk: (requirement_type, key)
                for pk, requirement_type, key in SkillRequirement.objects.filter(
                    skill=self.instance,
                ).values_list('pk', 'requirement_type', 'category_key')
            }
        for form in self.forms:
            if not form.cleaned_data or form.errors:
                continue
            pk = form.instance.pk
            if self._should_delete_form(form):
                keys.pop(pk, None)
            elif pk is None or form.has_changed():
                # Saved with the posted values. An unchanged form is not
                # saved, so its stored row is what stays.
                keys[form if pk is None else pk] = (
                    form.cleaned_data.get('requirement_type'),
                    category_key(form.cleaned_data.get('category_name', '')),
                )
        if len(keys) != len(set(keys.values())):
            raise ValidationError(
                'A skill cannot have two requirements of the same type '
                'for the same category (case is ignored).'
            )


class SkillRequirementInline(admin.TabularInline):
    model = SkillRequirement
    formset = SkillRequirementFormSet
    extra = 0
    fields = ('requirement_type', 'threshold', 'category_name')


class RefusedDeleteMixin:
    """Turn a delete refused after the confirmation page into a message.

    The confirmation page lists what blocks a delete, but an unlock or a new
    child can appear before the admin confirms. The delete then raises
    ProtectedError: instead of a 500, nothing is deleted and the admin is
    sent back with ``refused_delete_message``.
    """

    refused_delete_message = ''

    def delete_view(self, request, object_id, extra_context=None):
        try:
            return super().delete_view(request, object_id, extra_context)
        except ProtectedError:
            return self._refused_delete(request)

    def response_action(self, request, queryset):
        try:
            # delete_selected logs the deletions before delete_queryset and,
            # unlike delete_view, outside a transaction, so a refused delete
            # would otherwise stay in the history as done.
            with transaction.atomic():
                return super().response_action(request, queryset)
        except ProtectedError:
            return self._refused_delete(request)

    def _refused_delete(self, request):
        # Back to the same page; a confirmation page now lists the blocker.
        self.message_user(request, self.refused_delete_message, messages.ERROR)
        return HttpResponseRedirect(request.get_full_path())


@admin.register(Skill)
class SkillAdmin(RefusedDeleteMixin, admin.ModelAdmin):
    refused_delete_message = (
        'Nothing was deleted: a user unlocked the skill, or a child skill '
        'was added, in the meantime.'
    )
    list_display = ('name', 'parent', 'category', 'order')
    list_filter = ('category',)
    list_select_related = ('parent', 'category')
    search_fields = ('name',)
    inlines = (SkillRequirementInline,)

    @transaction.atomic
    def delete_queryset(self, request, queryset):
        # Skill.delete() locks first; a bulk delete has to as well, or its
        # cascade locks the requirement table before the skill table.
        lock_skill_catalog(CATALOG_WRITE_LOCK)
        super().delete_queryset(request, queryset)


@admin.register(UserSkill)
class UserSkillAdmin(RefusedDeleteMixin, admin.ModelAdmin):
    refused_delete_message = (
        'Nothing was deleted: the user unlocked a child skill in the '
        'meantime. Revoke the child first, or select it as well.'
    )
    list_display = ('user', 'skill', 'unlocked_at')
    list_filter = ('skill', 'unlocked_at')
    list_select_related = ('user', 'skill')
    readonly_fields = ('user', 'skill', 'unlocked_at')

    # Unlocks go through SkillService so requirements, the parent rule and
    # SkillUnlockedEvent are never bypassed.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_deleted_objects(self, objs, request):
        """Refuse to revoke a skill while the user keeps one of its children.

        Reported as protected objects, so the admin shows "Cannot delete" and
        lists the child unlocks to revoke first, or to select as well. This
        is not done in has_delete_permission, because deleting a user from
        the admin checks that permission for every UserSkill it cascades to.
        """
        objs = list(objs)
        deleted, model_count, perms_needed, protected = (
            super().get_deleted_objects(objs, request)
        )
        verbose_name = capfirst(UserSkill._meta.verbose_name)
        protected = list(protected) + [
            f'{verbose_name}: {row} (child of "{row.skill.parent}")'
            for row in kept_child_unlocks(objs)
        ]
        return deleted, model_count, perms_needed, protected

    def delete_model(self, request, obj):
        self.delete_queryset(request, UserSkill.objects.filter(pk=obj.pk))

    @transaction.atomic
    def delete_queryset(self, request, queryset):
        rows = list(queryset)
        # The locks unlock_skill takes, in the same order: the catalog so a
        # skill cannot be moved under the parent being revoked meanwhile, and
        # the character rows so a child cannot be unlocked between the
        # confirmation page and this delete. If a child was unlocked anyway,
        # nothing is deleted and the admin is told why.
        lock_skill_catalog(CATALOG_READ_LOCK)
        # One query, in user order. A missing character is not recreated:
        # there is then nothing an unlock could serialize on either way.
        list(
            Character.objects.select_for_update()
            .filter(user_id__in={row.user_id for row in rows})
            .order_by('user_id')
            .values_list('pk', flat=True)
        )
        kept = kept_child_unlocks(rows)
        if kept:
            raise ProtectedError(
                'A child skill was unlocked after the confirmation page.',
                set(kept),
            )
        UserSkill.objects.filter(pk__in=[row.pk for row in rows]).delete()


def kept_child_unlocks(revoked_rows):
    """Unlocks of children whose parent unlock is revoked but they are not."""
    revoked = {(row.user_id, row.skill_id) for row in revoked_rows}
    candidates = UserSkill.objects.filter(
        user_id__in={user_id for user_id, _ in revoked},
        skill__parent_id__in={skill_id for _, skill_id in revoked},
    ).select_related('user', 'skill__parent')
    return [
        row
        for row in candidates
        if (row.user_id, row.skill.parent_id) in revoked
        and (row.user_id, row.skill_id) not in revoked
    ]
