"""Reads of a user's rewards and coin history."""
from dataclasses import dataclass

from django.db.models import Q

from .models import Reward, WalletTransaction

TRANSACTION_PAGE_SIZE = 20


def rewards_for_user(user):
    """The user's rewards as the API and the shop show them: unarchived."""
    return Reward.objects.filter(
        user=user,
        archived_at__isnull=True,
    ).order_by('pk')


def transactions_for_user(user):
    """The coin history, newest first, with the redemption each spend paid."""
    return WalletTransaction.objects.filter(
        user=user,
    ).select_related('redemption').order_by('-created_at', '-pk')


@dataclass(frozen=True)
class TransactionPage:
    items: list[WalletTransaction]
    # Pass as ``before`` for the next page; None on the last one.
    next_before: int | None


def transaction_page(user, *, before=None, limit=TRANSACTION_PAGE_SIZE):
    """One page of ``transactions_for_user``, in its order.

    Pages go by the last row shown, not by offset, so coins earned meanwhile cannot
    show a row twice. A ``before`` that is not one of the user's rows reads as none,
    and says nothing about what exists. One query, two with a cursor.
    """
    rows = transactions_for_user(user)
    if before is not None:
        last_shown = rows.filter(pk=before).values_list(
            'created_at',
            flat=True,
        ).first()
        if last_shown is not None:
            rows = rows.filter(
                Q(created_at__lt=last_shown)
                | Q(created_at=last_shown, pk__lt=before),
            )
    found = list(rows[:limit + 1])
    page = found[:limit]
    return TransactionPage(
        items=page,
        next_before=page[-1].pk if len(found) > limit else None,
    )
