from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

USERNAME_VALIDATOR = RegexValidator(
    r'^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$',
    'Use 3 to 32 characters: letters, digits, dot, dash or underscore, starting with a letter or digit.')
RESERVED_NAMES = ('auto',)


def reserved_name(value):
    if value.strip().lower() in RESERVED_NAMES:
        raise ValidationError('That user name is reserved.')


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, username, email, password = None, **extra):
        if not username or not email:
            raise ValueError('A user name and an email address are required')
        user = self.model(username = username, email = self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using = self._db)
        return user

    def create_superuser(self, username, email, password = None, **extra):
        extra.update(is_staff = True, is_superuser = True, is_active = True, approved = True, email_verified = True)
        return self.create_user(username, email, password, **extra)

    def find(self, login):
        login = (login or '').strip()
        return (self.filter(username__iexact = login).first()
                or self.filter(email__iexact = login).first())


class User(AbstractBaseUser, PermissionsMixin):
    username       = models.CharField(max_length = 32, unique = True, validators = [USERNAME_VALIDATOR, reserved_name])
    email          = models.EmailField(max_length = 254, unique = True)
    is_active      = models.BooleanField(default = False, help_text = 'May sign in. Untick to disable the account.')
    approved       = models.BooleanField(default = False, help_text = 'Set when an administrator (or a confirmed email on an allowed domain) let the account in. A not-active account that was approved is a disabled one.')
    is_staff       = models.BooleanField(default = False, help_text = 'May use the /admin/ site.')
    email_verified = models.BooleanField(default = False)
    failed_logins  = models.PositiveIntegerField(default = 0)
    locked_until   = models.DateTimeField(null = True, blank = True)
    date_joined    = models.DateTimeField(default = timezone.now)

    objects        = UserManager()
    USERNAME_FIELD  = 'username'
    EMAIL_FIELD     = 'email'
    REQUIRED_FIELDS = ['email']

    class Meta:
        db_table = 'ci_users'
        constraints = [
            models.UniqueConstraint(Lower('username'), name = 'ci_users_username_ci'),
            models.UniqueConstraint(Lower('email'), name = 'ci_users_email_ci'),
        ]

    def __str__(self):
        return self.username

    @property
    def state(self):
        if self.is_active:
            return 'active'
        return 'disabled' if self.approved else 'pending'

    def approve(self):
        self.is_active = True
        self.approved  = True
        self.save(update_fields = ['is_active', 'approved'])

    def disable(self):
        self.is_active = False
        self.save(update_fields = ['is_active'])
