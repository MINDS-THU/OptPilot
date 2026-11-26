"""Resolve explicitly selected local agent authentication for a Harbor process.

Credentials stay in process environment and never enter frozen candidate specs.
"""
from pathlib import Path
import json
import os
import subprocess
from urllib.parse import urlsplit, urlunsplit


def docker_url(value):
    url = urlsplit(value)
    if url.hostname in ('127.0.0.1', 'localhost', '::1'):
        return urlunsplit((url.scheme, 'host.docker.internal' + (':' + str(url.port) if url.port else ''),
                           url.path, url.query, url.fragment))
    return value


def environment():
    env = dict(os.environ)
    if env.get('OPTPILOT_HARBOR_CODEX_LOGIN') == '1':
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        config_path = Path.home() / '.codex/config.toml'
        config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
        provider = config.get('model_providers', {}).get(config.get('model_provider', 'openai'), {})
        base = provider.get('base_url')
        if base:
            env['OPENAI_BASE_URL'] = docker_url(base)
            key = env.get(provider.get('env_key', ''), '')
            auth = provider.get('auth')
            if not key and isinstance(auth, dict) and auth.get('command'):
                # This is the user's configured credential helper, exactly as their Codex uses it.
                result = subprocess.run([auth['command'], *auth.get('args', [])], capture_output=True,
                                        text=True, timeout=min(auth.get('timeout_ms', 5000)/1000, 30))
                if result.returncode:
                    raise ValueError('The configured Codex credential helper failed; check the Codex login.')
                key = result.stdout.strip()
            if not key:
                raise ValueError('Codex custom provider has no usable API credential.')
            env['OPENAI_API_KEY'] = key
            env['CODEX_API_KEY'] = key
        else:
            env['CODEX_AUTH_JSON_PATH'] = str(Path.home() / '.codex/auth.json')
            env['CODEX_FORCE_AUTH_JSON'] = '1'
    if env.get('OPTPILOT_HARBOR_CLAUDE_SETTINGS') == '1':
        path = Path.home() / '.claude/settings.json'
        settings = json.loads(path.read_text()).get('env', {}) if path.exists() else {}
        for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
                     'ANTHROPIC_BASE_URL', 'CLAUDE_FORCE_OAUTH'):
            if settings.get(name):
                env[name] = docker_url(settings[name]) if name.endswith('BASE_URL') else str(settings[name])
        if not any(env.get(name) for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN')):
            raise ValueError('Harbor Claude Code needs ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or CLAUDE_CODE_OAUTH_TOKEN; none is available in the selected local settings.')
    return env
