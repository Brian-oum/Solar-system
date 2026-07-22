from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView as DjangoLoginView
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.contrib.auth.decorators import login_required
from django.views.generic import CreateView, ListView

from .forms import EngineerCreationForm, SignUpForm
from .models import User
from .permissions import AdminRequiredMixin


class LoginView(DjangoLoginView):
    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def form_invalid(self, form):
        messages.error(self.request, "Incorrect username or password.")
        return super().form_invalid(form)


class SignUpView(CreateView):
    """Public registration — always creates a Site Engineer account."""
    model = User
    form_class = SignUpForm
    template_name = "accounts/register.html"
    success_url = reverse_lazy("core:dashboard")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("core:dashboard")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object)
        messages.success(self.request, f"Welcome, {self.object.get_full_name() or self.object.username}.")
        return response


class UserListView(AdminRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    queryset = User.objects.all().order_by("role", "username")


class EngineerCreateView(AdminRequiredMixin, CreateView):
    model = User
    form_class = EngineerCreationForm
    template_name = "accounts/engineer_form.html"
    success_url = reverse_lazy("accounts:user_list")

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Site engineer '{self.object.username}' created.")
        return response

@login_required
def dashboard(request):
    """
    Single entry point after login. Renders a different template per role
    rather than redirecting to /admin/ or /engineer/ URLs, so the URL
    stays stable (bookmarkable) regardless of who's logged in.
    """
    template = (
        "core/dashboard_admin.html"
        if request.user.is_admin_role
        else "core/dashboard_engineer.html"
    )
    return render(request, template, {})
 