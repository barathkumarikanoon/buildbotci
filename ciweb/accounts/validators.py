from django.core.exceptions import ValidationError


class ContainsUserDetailsValidator:
    def validate(self, password, user = None):
        if user is None:
            return
        lowered = (password or '').lower()
        for value in (getattr(user, 'username', ''), (getattr(user, 'email', '') or '').split('@')[0]):
            value = (value or '').lower()
            if len(value) >= 3 and value in lowered:
                raise ValidationError('The password must not contain your user name or email.', code = 'password_contains_user_details')

    def get_help_text(self):
        return 'Your password must not contain your user name or email.'
