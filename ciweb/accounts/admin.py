from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm


class CIUserCreationForm(UserCreationForm):
    class Meta:
        model  = get_user_model()
        fields = ('username', 'email')


class CIUserChangeForm(UserChangeForm):
    class Meta:
        model  = get_user_model()
        fields = '__all__'


@admin.register(get_user_model())
class CIUserAdmin(UserAdmin):
    form          = CIUserChangeForm
    add_form      = CIUserCreationForm
    list_display  = ('username', 'email', 'state', 'email_verified', 'is_staff', 'last_login', 'locked')
    list_filter   = ('is_active', 'approved', 'email_verified', 'is_staff')
    search_fields = ('username', 'email')
    ordering      = ('username',)
    actions       = ('approve_users', 'disable_users', 'unlock_users')
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Account', {'fields': ('email', 'is_active', 'approved', 'email_verified')}),
        ('Access', {'fields': ('is_staff', 'is_superuser')}),
        ('Sign-in', {'fields': ('last_login', 'date_joined', 'failed_logins', 'locked_until')}),
    )
    add_fieldsets = ((None, {'classes': ('wide',), 'fields': ('username', 'email', 'password1', 'password2')}),)
    filter_horizontal = ()

    @admin.display(description = 'Locked', boolean = True)
    def locked(self, user):
        return bool(user.locked_until)

    @admin.display(description = 'State')
    def state(self, user):
        return user.state

    def save_model(self, request, obj, form, change):
        if not change:
            obj.is_active = obj.approved = obj.email_verified = True
        super().save_model(request, obj, form, change)

    @admin.action(description = 'Approve the selected accounts (let them sign in)')
    def approve_users(self, request, queryset):
        count = queryset.update(is_active = True, approved = True)
        self.message_user(request, '%d account(s) approved.' % count, messages.SUCCESS)

    @admin.action(description = 'Disable the selected accounts')
    def disable_users(self, request, queryset):
        count = queryset.exclude(pk = request.user.pk).update(is_active = False)
        self.message_user(request, '%d account(s) disabled.' % count, messages.SUCCESS)

    @admin.action(description = 'Unlock the selected accounts')
    def unlock_users(self, request, queryset):
        count = queryset.update(failed_logins = 0, locked_until = None)
        self.message_user(request, '%d account(s) unlocked.' % count, messages.SUCCESS)
