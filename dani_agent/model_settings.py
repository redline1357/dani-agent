"""Bounded model settings and the automatic choice of model and effort per build step.

Available models come from the confirmed CLIs themselves, with no test prompts: Codex's own model catalog
for the signed-in account, and Claude Code's documented aliases. A model that a real session refuses is
recorded and never offered again for that CLI version (a CLI update gives it a fresh chance).
"""
import datetime
import json
from pathlib import Path
import re
from .coordinator import APP, DATA, RUNS, atomic_json

PROBES = DATA / 'model_probes.json'
# CLI model names and aliases (e.g. gpt-6-astra, sonnet, claude-sonnet-4-5, sonnet[1m]); never an option flag.
MODEL_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,79}')
# Starting values. In 'auto' mode the models are the everyday choice and each step's difficulty sets the
# effort (and, for hard steps, the strongest available model). Settings saved before modes existed are 'manual'.
DEFAULTS = {'codex_model': 'gpt-6-astra', 'codex_effort': 'medium', 'claude_model': 'sonnet',
    'claude_effort': 'low', 'max_codex_sessions': 3, 'max_claude_sessions': 3,
    'seconds_per_session': 240, 'max_correction_cycles': 2, 'mode': 'auto'}
# Claude models for subscription plans, strongest first: Claude Code's aliases always mean the latest version,
# older versions are selected by full name. Models that need paid usage credits (e.g. Fable) are never used:
# Dani Agent stays within the subscription.
CLAUDE_MODELS = ('opus', 'sonnet', 'haiku', 'claude-opus-5', 'claude-sonnet-5', 'claude-opus-4-8', 'claude-opus-4-7',
                 'claude-opus-4-6', 'claude-sonnet-4-6', 'claude-haiku-4-5')
CLAUDE_LABELS = {'opus': 'Opus (latest)', 'sonnet': 'Sonnet (latest)', 'haiku': 'Haiku (latest)', 'claude-opus-5': 'Opus 5',
                 'claude-sonnet-5': 'Sonnet 5', 'claude-opus-4-8': 'Opus 4.8', 'claude-opus-4-7': 'Opus 4.7',
                 'claude-opus-4-6': 'Opus 4.6', 'claude-sonnet-4-6': 'Sonnet 4.6', 'claude-haiku-4-5': 'Haiku 4.5'}
DIFFICULTIES = ('simple', 'normal', 'hard')
EFFORT = {'simple': 'low', 'normal': 'medium', 'hard': 'high'}
# Real-project Codex turns routinely exceed two minutes; still a hard, approval-bound cap.
MAX_SECONDS_PER_SESSION = 300
TEST_SECONDS = 60
# Product work has no time limit (user decision, 2026-10-23): a session may take as long as it needs while it shows
# any sign of work. Only a session with no output at all for this long counts as stuck; it is restarted once.
SESSION_IDLE_SECONDS = 20 * 60
TEST_IDLE_SECONDS = 5 * 60      # product tests printing nothing for 5 minutes: an endless loop or a wait for input
JOB_IDLE_SECONDS = 30 * 60      # a whole build job with no activity anywhere


def workflow_ceiling(settings):
    """Outer kill deadline for one approved project task: every allowed session and test run, plus slack."""
    sessions = settings['max_codex_sessions'] + settings['max_claude_sessions']
    return sessions * settings['seconds_per_session'] + (settings['max_correction_cycles'] + 1) * TEST_SECONDS + 300


def model_name(value):
    if type(value) is not str or not MODEL_NAME.fullmatch(value):
        raise ValueError('INVALID_MODEL_NAME')
    return value


def probes():
    try:
        value = json.loads(PROBES.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return [p for p in value if type(p) is dict] if type(value) is list else []


def record_probe(provider, model, passed, evidence, *, legacy=False):
    """Called only by the trusted worker after an approved probe session (or the legacy import). Either way the
    result holds for the CLI version confirmed now; a CLI update needs a new probe."""
    version = APP['TOOLS'].get(provider, {}).get('version')
    if not version:
        raise RuntimeError('CONFIRM_THE_CLI_BEFORE_RECORDING_A_PROBE')
    entry = {'provider': provider, 'model': model_name(model), 'passed': passed is True, 'evidence': evidence,
        'cli_version': version, 'checked_at': datetime.datetime.now(datetime.UTC).isoformat()}
    if legacy:
        entry['source'] = 'LEGACY_SUCCESSFUL_RUN'

    atomic_json(PROBES, probes() + [entry])
    return entry


def latest_results(provider):
    """model -> did its latest session or probe with the currently confirmed CLI version work."""
    current, latest = APP['TOOLS'].get(provider, {}).get('version'), {}
    for p in probes():
        if p.get('provider') == provider and current and p.get('cli_version') == current:
            latest[p.get('model')] = p.get('passed') is True
    return latest


def codex_catalog():
    """The models Codex lists for the signed-in account (its own cache of the catalog), best first."""
    try:
        cache = json.loads((Path.home() / '.codex/models_cache.json').read_text(encoding='utf-8'))
        listed = [m for m in cache.get('models', []) if type(m) is dict and m.get('visibility') == 'list'
                  and type(m.get('slug')) is str and MODEL_NAME.fullmatch(m['slug'])]
    except (OSError, ValueError, AttributeError):
        return []
    return [m['slug'] for m in sorted(listed, key=lambda m: m.get('priority') if type(m.get('priority')) is int else 999)][:12]


def listed_models(provider):
    """No catalog, no assumption: without Codex's list only models that already worked are offered."""
    return codex_catalog() if provider == 'codex' else list(CLAUDE_MODELS)


def claude_credit_models():
    """Extra Claude models Claude Code offers that draw on paid usage credits, from its own local cache."""
    try:
        cache = json.loads((Path.home() / '.claude.json').read_text(encoding='utf-8'))
        paid = cache.get('cachedGrowthBookFeatures', {}).get('tengu_usage_overage_included_models') or []
        extra = cache.get('additionalModelOptionsCache') or []
        return sorted({o['description'].split(' \u00b7 ')[0] if type(o.get('description')) is str else str(o.get('label'))
                       for o in extra if type(o) is dict and (o.get('label') in paid or 'fable' in str(o.get('value')).lower())})
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return []


def refused_models(provider):
    return sorted(model for model, worked in latest_results(provider).items() if not worked)


def ranked_models(provider):
    """Usable models, best first: listed by the CLI (or worked before), never refused. Needs a confirmed CLI."""
    if not APP['TOOLS'].get(provider, {}).get('version'):
        return []
    latest = latest_results(provider)
    names = listed_models(provider) + [m for m, worked in latest.items() if worked]
    return [m for m in dict.fromkeys(names) if latest.get(m, True)]


def verified_models(provider):
    """Models Dani Agent may use for this provider now (see ranked_models)."""
    return set(ranked_models(provider))


def usable(role):
    """Usable models, best first."""
    allowed = verified_models(role)
    return [m for m in ranked_models(role) if m in allowed] or sorted(allowed)


def auto_settings():
    """Automatic mode with the everyday models: the defaults when available, otherwise the best listed."""
    value = dict(DEFAULTS)
    for role in ('codex', 'claude'):
        ranked = usable(role)
        if not ranked:
            raise ValueError('NO_' + role.upper() + '_MODEL_AVAILABLE_CONFIRM_AND_SIGN_IN_FIRST')
        if value[role + '_model'] not in ranked:
            value[role + '_model'] = ranked[0]
    return value


def for_step(settings, difficulty):
    """Model and effort for one step. Auto: the step's difficulty sets the effort, and a hard step gets the
    strongest available model. Manual (or older) settings are used unchanged."""
    if settings.get('mode') != 'auto':
        return dict(settings)
    if difficulty not in DIFFICULTIES:
        raise ValueError('INVALID_STEP_DIFFICULTY')
    try:   # today's everyday models: one refused since the product was created is replaced, so Build again works
        everyday = auto_settings()
    except ValueError:
        everyday = settings
    value = dict(settings, codex_model=everyday['codex_model'], claude_model=everyday['claude_model'],
                 codex_effort=EFFORT[difficulty], claude_effort=EFFORT[difficulty])
    if difficulty == 'hard':
        value['codex_model'] = (usable('codex') or [settings['codex_model']])[0]
        value['claude_model'] = (usable('claude') or [settings['claude_model']])[0]
    return value


REFUSAL = re.compile(r'model\b.{0,80}?\b(does not exist|not (?:available|supported|enabled|included) (?:on|for|with|in) '
                     r'(?:your|this) (?:plan|account|subscription)|not supported when using|(?:do|does) not have access|no access to|not_found_error|'
                     r'invalid model)|not_found_error.{0,40}\bmodel\b', re.I | re.S)
TEMPORARY = re.compile(r'temporar|overload|try again|rate.?limit|capacity|timeout|timed out|unavailable right now', re.I)


def model_refused(out):
    """The session failed because the account's plan does not include the requested model."""
    texts = [str(out.get('error') or ''), json.dumps(out.get('sanitized_error_contexts') or [])[:8000]]   # Codex: stderr
    for event in out.get('events', []):
        if type(event) is dict and (event.get('type') in ('error', 'turn.failed') or event.get('is_error')
                                    or str(event.get('subtype') or '').startswith('error')):
            texts.append(json.dumps(event)[:4000])
    return any(REFUSAL.search(text) and not TEMPORARY.search(text) for text in texts)


def validate(value, *, evidence=True):
    required = set(DEFAULTS) - {'mode'}
    if type(value) is not dict or set(value) - {'mode'} != required or value.get('mode', 'manual') not in ('auto', 'manual'):
        raise ValueError('INVALID_MODEL_SETTINGS')
    model_name(value['codex_model']), model_name(value['claude_model'])
    if evidence and (value['codex_model'] not in verified_models('codex')
            or value['claude_model'] not in verified_models('claude')):
        raise ValueError('MODEL_NOT_SUBSCRIPTION_VERIFIED_NO_AUTOMATIC_UPGRADE')
    if value['codex_effort'] not in EFFORT.values() or value['claude_effort'] not in EFFORT.values():
        raise ValueError('INVALID_REASONING_EFFORT')
    for key, low, high in (('max_codex_sessions', 1, 3), ('max_claude_sessions', 1, 3),
            ('seconds_per_session', 15, MAX_SECONDS_PER_SESSION), ('max_correction_cycles', 0, 2)):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError('INVALID_SESSION_LIMIT_' + key)
    if min(value['max_codex_sessions'], value['max_claude_sessions']) < value['max_correction_cycles'] + 1:
        raise ValueError('SESSION_BUDGET_TOO_SMALL_FOR_CORRECTIONS')
    return dict(value)


def legacy_evidence(value, candidates=None):
    """An earlier successful, human-approved live run that used exactly these two models."""
    candidates = candidates if candidates is not None else sorted(
        (p.parent for root in (RUNS, APP['WORK_RUNS']) for p in root.glob('*/result.json')), reverse=True)
    for candidate in candidates:
        try:
            result = json.loads((candidate / 'result.json').read_text())
            if result.get('scope') not in ('LIVE_CORRECTION_VALIDATION', 'LIVE_COLLABORATION', 'LIVE_PROJECT_DEVELOPMENT') or result.get('verdict') != 'PASS':
                continue
            if not (candidate / 'artifacts/local_live_approval.json').is_file():
                continue
            specs = [json.loads(p.read_text()) for p in (candidate / 'artifacts').glob('cycle_*/*.spec.json')]
            if all(any(s.get('kind') == ('codex' if role == 'codex' else 'claude-review')
                    and '--model' in s.get('argv', []) and s['argv'][s['argv'].index('--model') + 1] == value[role + '_model']
                    for s in specs) for role in ('codex', 'claude')):
                return candidate
        except (OSError, ValueError, IndexError, KeyError, TypeError):
            continue
    return None


def availability(value, *, evidence=None):
    validate(value, evidence=False)
    # Account entitlement cannot be inferred from a model name or API catalog. Reuse actual
    # successful subscription sessions (probes or approved runs) and pinned CLI/version gates.
    found = legacy_evidence(value, None if evidence is None else [evidence])
    if found is None and evidence is None and all(value[r + '_model'] in verified_models(r) for r in ('codex', 'claude')):
        found = PROBES
    if found is None:
        raise RuntimeError('SUCCESSFUL_SUBSCRIPTION_MODEL_EVIDENCE_REQUIRED')
    return {'status': 'PREVIOUSLY_SUBSCRIPTION_VERIFIED', 'evidence': str(found),
        'live_current_quota': 'UNKNOWN', 'api_fallback': False,
        'reasoning_effort': 'VALIDATED_CLI_SETTING_NOT_A_CURRENT_ENTITLEMENT_CHECK',
        'note': 'Pinned CLI/authentication preflight is still required before execution. Entitlements can change.'}


def import_legacy_evidence():
    """For an installation that already ran approved builds: once per CLI, as soon as it is confirmed and has no
    probe of its own, its default model counts as verified for the version confirmed now."""
    pending = [role for role in ('codex', 'claude') if APP['TOOLS'].get(role, {}).get('version')
               and not any(p.get('provider') == role for p in probes())]
    found = legacy_evidence(DEFAULTS) if pending else None
    if found is None:
        return []
    return [record_probe(role, DEFAULTS[role + '_model'], True, str(found), legacy=True) for role in pending]
