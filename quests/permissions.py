from rest_framework.permissions import BasePermission


class IsOwner(BasePermission):
    message = 'You can only access your own objects.'

    def has_object_permission(self, request, view, obj):
        return getattr(obj, 'user_id', None) == request.user.id
