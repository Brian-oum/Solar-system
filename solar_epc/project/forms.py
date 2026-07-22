from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import User


class EngineerCreationForm(UserCreationForm):
    """Used by Admins to create Site Engineer accounts. Role is fixed,
    not exposed as a field, so an admin can't accidentally create
    another admin from this form."""

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "phone_number")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.SITE_ENGINEER
        if commit:
            user.save()
        return user


class SignUpForm(UserCreationForm):
    """
    Public self-registration. Always creates a Site Engineer account —
    role is never a field on this form, so there's no way to submit
    role=admin from the outside. Admin accounts are provisioned by an
    existing Admin, not through open sign-up.
    """

    email = forms.EmailField(required=True)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "phone_number")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.SITE_ENGINEER
        if commit:
            user.save()
        return user