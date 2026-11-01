"""Reads of a user's rewards and coin history."""
from .models import Reward, WalletTransaction


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
