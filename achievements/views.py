from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated

# Re-exported: the tests and other callers import it from here.
from .selectors import achievements_for_user
from .serializers import AchievementSerializer

__all__ = (
    'AchievementListView',
    'UnlockedAchievementListView',
    'achievements_for_user',
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
