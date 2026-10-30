"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.conf import settings
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter

from config.health import healthz
from core.views import CharacterView
from core.auth_views import RegisterView, TokenLoginView, TokenLogoutView
from achievements.views import AchievementListView, UnlockedAchievementListView
from bosses.views import BossViewSet
from quests.views import QuestCategoryViewSet, QuestViewSet
from skills.views import SkillTreeView, SkillUnlockView
from wallet.views import RewardViewSet, WalletTransactionListView


router = DefaultRouter()
router.register('quests', QuestViewSet, basename='quest')
router.register('quest-categories', QuestCategoryViewSet, basename='quest-category')
router.register('rewards', RewardViewSet, basename='reward')
router.register('bosses', BossViewSet, basename='boss')

urlpatterns = [
	path('healthz/', healthz, name='healthz'),
    path('admin/', admin.site.urls),
    path('api/auth/login/', TokenLoginView.as_view(), name='api-token-login'),
    path('api/auth/logout/', TokenLogoutView.as_view(), name='api-token-logout'),
    path('api/auth/register/', RegisterView.as_view(), name='api-register'),
    path('api/', include(router.urls)),
    path('api/character/', CharacterView.as_view(), name='character'),
    path(
        'api/achievements/',
        AchievementListView.as_view(),
        name='achievement-list',
    ),
    path(
        'api/achievements/unlocked/',
        UnlockedAchievementListView.as_view(),
        name='achievement-unlocked-list',
    ),
    path('api/skills/tree/', SkillTreeView.as_view(), name='skill-tree'),
    path(
        'api/skills/<int:skill_id>/unlock/',
        SkillUnlockView.as_view(),
        name='skill-unlock',
    ),
    path(
        'api/wallet/transactions/',
        WalletTransactionListView.as_view(),
        name='wallet-transaction-list',
    ),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path(
        'api/docs/',
        SpectacularSwaggerView.as_view(url_name='schema'),
        name='swagger-ui',
    ),
]

if settings.DEBUG:
    from django.contrib.staticfiles.urls import staticfiles_urlpatterns

    urlpatterns += staticfiles_urlpatterns()
