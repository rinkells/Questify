from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from django.db.models import Exists, OuterRef

from .models import Achievement, UserAchievement
from .serializers import AchievementSerializer


def achievements_for_user(user):
    return Achievement.objects.order_by('pk').annotate(
        is_unlocked=Exists(
            UserAchievement.objects.filter(
                user=user,
                achievement_id=OuterRef('pk'),
            )
        )
    )


class AchievementListView(ListAPIView):
    serializer_class = AchievementSerializer
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        return achievements_for_user(self.request.user)


class UnlockedAchievementListView(ListAPIView):
    serializer_class = AchievementSerializer
    permission_classes = (IsAuthenticated,)

    def get_queryset(self):
        return achievements_for_user(self.request.user).filter(is_unlocked=True)
