"""Tests for the caller-directed env-injection secret channel (direct channel).

Covers the embedded-caller path that lets a protocol bridge hand a request
credential (e.g. ``MULTICA_TOKEN``) to sandbox subprocesses with no skill in
the loop:

  trusted embedded caller passes ``direct_env_secrets`` to ``client.stream()``
    -> reserved ``__direct_env_secrets`` run-context key
    -> bash tool merges it into the per-call ``env`` overlay
    -> ``build_sandbox_env`` layers it over the scrubbed inherited environ

Negative assertions pin the gate: request secrets supplied under
``context.secrets`` still require a skill declaration, host credentials are
still scrubbed, and the gateway boundary still strips caller-seeded dunder
keys. The direct channel adds a delivery path for trusted embedded callers;
it does not weaken the stripping policy.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from deerflow.config.authorization_config import AuthorizationConfig
from deerflow.runtime.secret_context import (
    ACTIVE_SECRETS_CONTEXT_KEY,
    DIRECT_ENV_SECRETS_CONTEXT_KEY,
    REDACTED_CONTEXT_KEYS,
    coerce_secret_pairs,
    read_direct_env_secrets,
    redact_config_secrets,
    redact_secret_context_keys,
)
from deerflow.sandbox.local.local_sandbox import LocalSandbox
from deerflow.sandbox.tools import bash_tool

#: Fake credential values used throughout — never a real token.
_FAKE_TOKEN = "fake-multica-token-for-tests"


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_client.py — standard embedded-client harness)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_app_config():
    """Provide a minimal AppConfig mock."""
    model = MagicMock()
    model.name = "test-model"
    model.model = "test-model"
    model.supports_thinking = False
    model.supports_reasoning_effort = False
    model.model_dump.return_value = {"name": "test-model", "use": "langchain_openai:ChatOpenAI"}

    config = MagicMock()
    config.models = [model]
    config.token_usage.enabled = False
    config.skills.deferred_discovery = False
    config.skills.container_path = "/mnt/skills"
    config.tool_search.enabled = False
    config.database.checkpoint_channel_mode = "full"
    config.database.checkpoint_delta.snapshot_frequency = 10
    config.authorization = AuthorizationConfig(enabled=False)
    return config


@pytest.fixture
def client(mock_app_config, tmp_path):
    """Create a DeerFlowClient with mocked config loading."""
    import deerflow.skills.storage as _storage_mod
    from deerflow.skills.storage.local_skill_storage import LocalSkillStorage

    _storage_mod._default_skill_storage = LocalSkillStorage(host_path=str(tmp_path))
    with patch("deerflow.client.get_app_config", return_value=mock_app_config):
        from deerflow.client import DeerFlowClient

        return DeerFlowClient()


def _make_agent_mock(chunks: list[dict]):
    agent = MagicMock()
    agent.stream.return_value = iter(chunks)
    return agent


# ---------------------------------------------------------------------------
# Carrier helpers: coerce / read / redact
# ---------------------------------------------------------------------------


class TestCarrierHelpers:
    def test_coerce_secret_pairs_filters_non_string_pairs(self):
        assert coerce_secret_pairs({"A": "x", "B": 123, 4: "y"}) == {"A": "x"}

    def test_coerce_secret_pairs_malformed_inputs(self):
        assert coerce_secret_pairs("not-a-dict") == {}
        assert coerce_secret_pairs(None) == {}
        assert coerce_secret_pairs({}) == {}

    def test_read_direct_env_secrets_from_context(self):
        context = {"thread_id": "t1", DIRECT_ENV_SECRETS_CONTEXT_KEY: {"MULTICA_TOKEN": _FAKE_TOKEN}}
        assert read_direct_env_secrets(context) == {"MULTICA_TOKEN": _FAKE_TOKEN}

    def test_read_direct_env_secrets_missing_or_malformed(self):
        assert read_direct_env_secrets({}) == {}
        assert read_direct_env_secrets({DIRECT_ENV_SECRETS_CONTEXT_KEY: "nope"}) == {}
        assert read_direct_env_secrets(None) == {}

    def test_direct_env_secrets_in_redaction_allowlist(self):
        assert DIRECT_ENV_SECRETS_CONTEXT_KEY in REDACTED_CONTEXT_KEYS

        context = {"thread_id": "t1", DIRECT_ENV_SECRETS_CONTEXT_KEY: {"MULTICA_TOKEN": _FAKE_TOKEN}}
        assert DIRECT_ENV_SECRETS_CONTEXT_KEY not in redact_secret_context_keys(context)

        config = {"context": dict(context)}
        redacted = redact_config_secrets(config)
        assert DIRECT_ENV_SECRETS_CONTEXT_KEY not in redacted["context"]

    def test_gateway_strips_caller_seeded_direct_channel_key(self):
        """HTTP callers cannot seed the direct channel: dunder context keys are
        stripped at the gateway boundary, so the client kwarg is the only writer."""
        from app.gateway.services import build_run_config

        config = build_run_config(
            "thread-1",
            {
                "context": {
                    "secrets": {"ERP_TOKEN": "v"},
                    DIRECT_ENV_SECRETS_CONTEXT_KEY: {"MULTICA_TOKEN": "forged"},
                }
            },
            None,
        )
        assert config["context"]["secrets"] == {"ERP_TOKEN": "v"}
        assert DIRECT_ENV_SECRETS_CONTEXT_KEY not in config["context"]


# ---------------------------------------------------------------------------
# Embedded client entry: kwarg -> reserved run-context key
# ---------------------------------------------------------------------------


class TestClientCarriesDirectEnvSecrets:
    def test_stream_carries_direct_env_secrets_in_context(self, client):
        agent = _make_agent_mock([{"messages": [AIMessage(content="ok", id="ai-1")]}])

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            list(client.stream("hi", thread_id="t1", direct_env_secrets={"MULTICA_TOKEN": _FAKE_TOKEN}))

        call_kwargs = agent.stream.call_args.kwargs
        assert call_kwargs["context"][DIRECT_ENV_SECRETS_CONTEXT_KEY] == {"MULTICA_TOKEN": _FAKE_TOKEN}
        # Never mirrored into configurable (trace backends surface it there).
        assert DIRECT_ENV_SECRETS_CONTEXT_KEY not in (call_kwargs["config"].get("configurable") or {})

    def test_stream_omits_key_without_kwarg(self, client):
        agent = _make_agent_mock([{"messages": [AIMessage(content="ok", id="ai-1")]}])

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            list(client.stream("hi", thread_id="t1"))

        assert DIRECT_ENV_SECRETS_CONTEXT_KEY not in agent.stream.call_args.kwargs["context"]

    def test_stream_filters_non_string_pairs(self, client):
        agent = _make_agent_mock([{"messages": [AIMessage(content="ok", id="ai-1")]}])

        with (
            patch.object(client, "_ensure_agent"),
            patch.object(client, "_agent", agent),
        ):
            list(client.stream("hi", thread_id="t1", direct_env_secrets={"MULTICA_TOKEN": 123, 4: "v"}))

        assert DIRECT_ENV_SECRETS_CONTEXT_KEY not in agent.stream.call_args.kwargs["context"]


# ---------------------------------------------------------------------------
# bash tool: no skill activation -> direct secrets still reach the subprocess env
# ---------------------------------------------------------------------------


def _bash_capture_env(monkeypatch: pytest.MonkeyPatch, runtime: SimpleNamespace, command: str) -> dict:
    captured: dict = {}

    class _Sandbox:
        def execute_command(self, command, env=None, timeout=None):
            captured["env"] = env
            return "done"

    monkeypatch.setattr("deerflow.sandbox.tools.ensure_sandbox_initialized", lambda runtime: _Sandbox())
    monkeypatch.setattr("deerflow.sandbox.tools.ensure_thread_directories_exist", lambda runtime: None)
    bash_tool.func(runtime=runtime, description="run", command=command)
    return captured["env"]


class TestBashToolDirectChannel:
    def test_direct_secrets_reach_env_without_skill_activation(self, monkeypatch):
        """Positive assertion for the direct channel. With no skill declared or
        activated, a caller-directed secret still lands in the subprocess env.
        Removing the direct-channel merge (or the client kwarg handling) makes
        this test fail — proving delivery rides the explicit channel, not a
        weakened strip policy."""
        runtime = SimpleNamespace(
            state={"sandbox": {"sandbox_id": "aio:xyz"}},
            context={"thread_id": "t1", DIRECT_ENV_SECRETS_CONTEXT_KEY: {"MULTICA_TOKEN": _FAKE_TOKEN}},
            config={},
        )
        env = _bash_capture_env(monkeypatch, runtime, "multica issue list")
        assert env == {"MULTICA_TOKEN": _FAKE_TOKEN}

    def test_undeclared_request_secrets_stay_out_of_env(self, monkeypatch):
        """Gate preservation: secrets supplied under ``context.secrets`` without a
        skill declaration must NOT leak into the subprocess env — the direct
        channel delivers exactly the names the caller directed, nothing more."""
        runtime = SimpleNamespace(
            state={"sandbox": {"sandbox_id": "aio:xyz"}},
            context={
                "thread_id": "t1",
                "secrets": {"GITHUB_TOKEN": "req-gh", "DATABASE_URL": "postgres://u:p@h/db"},
                DIRECT_ENV_SECRETS_CONTEXT_KEY: {"MULTICA_TOKEN": _FAKE_TOKEN},
            },
            config={},
        )
        env = _bash_capture_env(monkeypatch, runtime, "multica issue list")
        assert env == {"MULTICA_TOKEN": _FAKE_TOKEN}

    def test_skill_resolved_binding_wins_on_collision(self, monkeypatch):
        runtime = SimpleNamespace(
            state={"sandbox": {"sandbox_id": "aio:xyz"}},
            context={
                "thread_id": "t1",
                ACTIVE_SECRETS_CONTEXT_KEY: {"ERP_TOKEN": "from-skill"},
                DIRECT_ENV_SECRETS_CONTEXT_KEY: {"MULTICA_TOKEN": _FAKE_TOKEN, "ERP_TOKEN": "from-direct"},
            },
            config={},
        )
        env = _bash_capture_env(monkeypatch, runtime, "run report")
        assert env == {"MULTICA_TOKEN": _FAKE_TOKEN, "ERP_TOKEN": "from-skill"}

    def test_no_carriers_env_stays_none(self, monkeypatch):
        runtime = SimpleNamespace(
            state={"sandbox": {"sandbox_id": "aio:xyz"}},
            context={"thread_id": "t1"},
            config={},
        )
        assert _bash_capture_env(monkeypatch, runtime, "ls") is None


class TestLocalSandboxDirectOverlay:
    def test_direct_overlay_wins_over_scrub(self, monkeypatch):
        """End-to-end local path: inherited host credentials are scrubbed, the
        caller-directed overlay wins for its names, benign vars survive."""
        import deerflow.sandbox.local.local_sandbox as local_sandbox

        captured: dict = {}

        def fake_run_posix(args, timeout, env=None):
            captured["env"] = env
            return ("", "", 0, False)

        runner = "_run_windows_command" if os.name == "nt" else "_run_posix_command"
        monkeypatch.setattr(LocalSandbox, runner, staticmethod(fake_run_posix))
        monkeypatch.setattr(LocalSandbox, "_get_shell", staticmethod(lambda: "/bin/bash"))
        monkeypatch.setattr(
            local_sandbox.os,
            "environ",
            {
                "PATH": "/usr/bin",
                "EXISTING": "kept",
                "MULTICA_TOKEN": "host-stale-value",
                "GITHUB_TOKEN": "host-gh-value",
                "DATABASE_URL": "postgres://u:p@h/db",
            },
        )

        LocalSandbox("local:t").execute_command("echo ok", env={"MULTICA_TOKEN": _FAKE_TOKEN})

        env = captured["env"]
        assert env["MULTICA_TOKEN"] == _FAKE_TOKEN
        assert "GITHUB_TOKEN" not in env
        assert "DATABASE_URL" not in env
        assert env["EXISTING"] == "kept"
        assert env["PATH"] == "/usr/bin"


# ---------------------------------------------------------------------------
# Skill middleware non-interference
# ---------------------------------------------------------------------------


def test_secret_binding_recompute_leaves_direct_channel_intact():
    """The middleware owns the active-skill set only: its per-call recompute
    pops a stale binding but must never touch the direct channel's key."""
    from deerflow.agents.middlewares.skill_activation_middleware import SkillActivationMiddleware

    middleware = SkillActivationMiddleware(slash_source_owner_token="test-owner-token")
    context = {
        ACTIVE_SECRETS_CONTEXT_KEY: {"STALE_TOKEN": "stale-v"},
        DIRECT_ENV_SECRETS_CONTEXT_KEY: {"MULTICA_TOKEN": _FAKE_TOKEN},
    }
    request = SimpleNamespace(runtime=SimpleNamespace(context=context))

    middleware._resolve_secret_bindings(request, None, hook="test")

    assert context[DIRECT_ENV_SECRETS_CONTEXT_KEY] == {"MULTICA_TOKEN": _FAKE_TOKEN}
    assert ACTIVE_SECRETS_CONTEXT_KEY not in context
