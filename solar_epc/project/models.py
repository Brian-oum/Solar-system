from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Two roles for now, matching the access model we designed:
      - SITE_ENGINEER: full lifecycle ownership of their own assigned projects
      - ADMIN: system-wide visibility, user management, oversight
    """

    class Role(models.TextChoices):
        SITE_ENGINEER = "site_engineer", "Site engineer"
        ADMIN = "admin", "Admin"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.SITE_ENGINEER)
    phone_number = models.CharField(max_length=20, blank=True)

    @property
    def is_site_engineer(self):
        return self.role == self.Role.SITE_ENGINEER

    @property
    def is_admin_role(self):
        return self.role == self.Role.ADMIN

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"