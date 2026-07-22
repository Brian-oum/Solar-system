"""
Role-based access control for the two-role model.

Two ways to use these, pick whichever fits the view:

    @admin_required
    def some_view(request): ...

    class SomeView(AdminRequiredMixin, View): ...

Both redirect anonymous users to login, and raise 403 for a logged-in
user with the wrong role (rather than silently redirecting), so a Site
Engineer hitting an admin-only URL gets a clear "not allowed" instead of
a confusing bounce.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied

from .models import User


def role_required(*roles):
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped(request, *args, **kwargs):
            if request.user.role not in roles:
                raise PermissionDenied("You don't have access to this page.")
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


def admin_required(view_func):
    return role_required(User.Role.ADMIN)(view_func)


def site_engineer_required(view_func):
    return role_required(User.Role.SITE_ENGINEER)(view_func)


class RoleRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    allowed_roles = ()
    raise_exception = True

    def test_func(self):
        return self.request.user.role in self.allowed_roles


class AdminRequiredMixin(RoleRequiredMixin):
    allowed_roles = (User.Role.ADMIN,)


class SiteEngineerRequiredMixin(RoleRequiredMixin):
    allowed_roles = (User.Role.SITE_ENGINEER,)