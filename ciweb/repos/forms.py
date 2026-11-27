from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from hub import repos as hub_repos
from hub.adapters import ADAPTERS
from hub.adapters.base import BOOL, CHOICE, INT, LINES
from ciweb.repos.models import Branch, RepoProfile


def option_name(kind, name):
    return 'opt_%s_%s' % (kind, name)


class RepoProfileForm(forms.ModelForm):
    """The repo profile. The settings of the test suite types come from `Adapter.fields`: a new type adds its own
    fields here without any change to this form."""

    class Meta:
        model  = RepoProfile
        fields = ['key', 'name', 'description', 'enabled', 'adapter', 'repo_dir', 'checkout_name', 'python', 'env_text',
                  'schedule', 'cron_hour', 'cron_minute', 'worker_count', 'worker_names',
                  'max_runs', 'max_shards', 'max_parallel', 'step_timeout', 'step_max_time']
        widgets = {'env_text': forms.Textarea(attrs = {'rows': 4, 'spellcheck': 'false', 'placeholder': 'DATADIR=/data/indianlaw'}),
                   'key': forms.TextInput(attrs = {'autocomplete': 'off'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['key'].disabled = bool(self.instance.pk)
        saved = (self.instance.options or {}) if self.instance.pk else {}
        for kind, cls in ADAPTERS.items():
            current = cls.with_defaults(saved if self.instance.adapter == kind else {})
            for f in cls.fields:
                name = option_name(kind, f.name)
                if f.kind == BOOL:
                    field = forms.BooleanField(required = False, label = f.label, help_text = f.help, initial = current[f.name])
                elif f.kind == CHOICE:
                    field = forms.ChoiceField(required = False, label = f.label, help_text = f.help, choices = f.choices, initial = current[f.name])
                elif f.kind == INT:
                    field = forms.CharField(required = False, label = f.label, help_text = f.help, initial = current[f.name])
                else:
                    widget = forms.Textarea(attrs = {'rows': 6, 'spellcheck': 'false'}) if f.kind == LINES else forms.TextInput()
                    if f.placeholder:
                        widget.attrs['placeholder'] = f.placeholder
                    field = forms.CharField(required = False, label = f.label, help_text = f.help, initial = current[f.name], widget = widget)
                self.fields[name] = field
        self.option_sections = []

    def sections(self):
        """The settings of each test suite type, for the template (it shows only the chosen type's)."""
        return [{'kind': kind, 'label': cls.label, 'summary': cls.summary, 'best_for': cls.best_for,
                 'fields': [self[option_name(kind, f.name)] for f in cls.fields]} for kind, cls in ADAPTERS.items()]

    def clean_worker_count(self):
        count = self.cleaned_data['worker_count']
        if not 1 <= count <= 40:
            raise forms.ValidationError('Use between 1 and 40 workers.')
        return count

    def clean_env_text(self):
        text = self.cleaned_data['env_text']
        try:
            hub_repos.parse_env_lines(text)
        except ValueError as exc:
            raise forms.ValidationError(str(exc))
        return text

    def clean(self):
        data = super().clean()
        kind = data.get('adapter')
        if kind in ADAPTERS:
            raw = {f.name: data.get(option_name(kind, f.name)) for f in ADAPTERS[kind].fields}
            options, problems = ADAPTERS[kind].clean_options(raw)
            for name, message in problems.items():
                self.add_error(option_name(kind, name), message)
            data['options'] = options
        return data

    def save(self, commit = True):
        profile = super().save(commit = False)
        profile.options = self.cleaned_data.get('options', {})
        if commit:
            profile.save()
        return profile

    def cross_check(self, branches):
        """Problems that need the branches and the other profiles; added to the form. Returns True when there are none."""
        if self.errors:
            return False
        draft = self.save(commit = False)
        taken = set()
        for other in RepoProfile.objects.exclude(pk = draft.pk):
            taken.update(other.worker_list())
        config = draft.to_config(branches = branches)
        ok = True
        for name, message in hub_repos.validate(config, taken_workers = taken):
            ok = False
            self.add_error(name if name in self.fields else None, message)
        return ok


class BranchForm(forms.ModelForm):
    class Meta:
        model  = Branch
        fields = ['name', 'scheduled', 'cron_hour', 'cron_minute']
        widgets = {'name': forms.TextInput(attrs = {'placeholder': 'main', 'autocomplete': 'off'}),
                   'cron_hour': forms.TextInput(attrs = {'placeholder': 'default', 'autocomplete': 'off', 'size': 6}),
                   'cron_minute': forms.TextInput(attrs = {'placeholder': 'default', 'autocomplete': 'off', 'size': 6})}

    def _clean_cron(self, name, low, high):
        value = (self.cleaned_data.get(name) or '').strip()
        error = value and hub_repos.validate_cron(value, low, high)
        if error:
            raise forms.ValidationError(error)
        return value

    def clean_cron_hour(self):
        return self._clean_cron('cron_hour', 0, 23)

    def clean_cron_minute(self):
        return self._clean_cron('cron_minute', 0, 59)


class BaseBranchFormSet(BaseInlineFormSet):
    def kept(self):
        """The branches that will exist after saving: [Branch] built from the valid, not deleted forms."""
        found = []
        for form in self.forms:
            if not form.is_valid() or not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue
            if form.cleaned_data.get('name'):
                found.append(Branch(name = form.cleaned_data['name'], scheduled = form.cleaned_data.get('scheduled', False),
                                    cron_hour = form.cleaned_data.get('cron_hour', ''), cron_minute = form.cleaned_data.get('cron_minute', '')))
        return found


BranchFormSet = inlineformset_factory(RepoProfile, Branch, form = BranchForm, formset = BaseBranchFormSet,
                                      extra = 1, can_delete = True)
