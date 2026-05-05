"""Tests for touch_grass.setup_wizard.

The wizard is fully driven by injectable callables (input_fn, output_fn,
browser_fn, validate functions on each Provider), so we exercise it end-to-end
without any real network or stdin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from touch_grass.setup_wizard import (
    Provider,
    _read_env,
    _write_env_key,
    run_setup,
)


def _make_provider(
    env_var: str = "FAKE_KEY",
    name: str = "Fake",
    *,
    validate_result: tuple[bool, str] = (True, "OK"),
    optional: bool = False,
) -> Provider:
    return Provider(
        env_var=env_var,
        name=name,
        signup_url="https://example.test/signup",
        instructions="1. Sign in\n2. Make a key",
        validate=lambda key: validate_result,
        optional=optional,
    )


class _Inputs:
    """Replay queued user inputs in order."""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"Wizard asked extra prompt: {prompt!r}")
        return self.answers.pop(0)


class _Output:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _no_browser(url: str) -> None:
    pass  # explicitly do not open a real browser in tests


def test_read_env_missing_returns_empty(tmp_path: Path):
    assert _read_env(tmp_path / "absent.env") == {}


def test_read_env_parses_key_value_lines(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        '# comment\nFOO=bar\n  BAZ = "quoted value"  \nEMPTY=\nnoequals\n',
        encoding="utf-8",
    )
    parsed = _read_env(p)
    assert parsed == {"FOO": "bar", "BAZ": "quoted value", "EMPTY": ""}


def test_write_env_key_creates_file_and_preserves_others(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("EXISTING=keep_me\n", encoding="utf-8")
    _write_env_key(p, "NEW", "value123")
    parsed = _read_env(p)
    assert parsed == {"EXISTING": "keep_me", "NEW": "value123"}


def test_write_env_key_creates_missing_file(tmp_path: Path):
    p = tmp_path / "subdir" / ".env"
    _write_env_key(p, "ONLY", "value")
    assert _read_env(p) == {"ONLY": "value"}


def test_run_setup_happy_path_saves_validated_key(tmp_path: Path):
    env = tmp_path / ".env"
    provider = _make_provider(validate_result=(True, "OK"))

    inputs = _Inputs(
        [
            "y",  # configure?
            "n",  # open browser? (skip)
            "PASTED_KEY",  # paste key
        ]
    )
    out = _Output()

    rc = run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(provider,),
    )

    assert rc == 0
    assert _read_env(env) == {"FAKE_KEY": "PASTED_KEY"}
    assert "Validated and saved" in out.text
    assert "saved" in out.text


def test_run_setup_allow_unvalidated_saves_after_confirmation(tmp_path: Path):
    env = tmp_path / ".env"
    provider = _make_provider(validate_result=(False, "Network error: timeout"))

    inputs = _Inputs(
        [
            "y",  # configure?
            "n",  # browser?
            "PASTED_KEY",  # paste
            "y",  # save anyway?
        ]
    )
    out = _Output()

    rc = run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(provider,),
        allow_unvalidated=True,
    )

    assert rc == 0
    assert _read_env(env) == {"FAKE_KEY": "PASTED_KEY"}
    assert "saved unvalidated" in out.text


def test_run_setup_allow_unvalidated_can_decline(tmp_path: Path):
    env = tmp_path / ".env"
    provider = _make_provider(validate_result=(False, "denied"))

    inputs = _Inputs(["y", "n", "PASTED_KEY", "n"])  # decline save-anyway
    out = _Output()

    run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(provider,),
        allow_unvalidated=True,
    )

    assert _read_env(env) == {}


def test_run_setup_keyboard_interrupt_returns_1_and_keeps_prior_saves(tmp_path: Path):
    env = tmp_path / ".env"
    p_first = _make_provider(env_var="A", name="ProvA", validate_result=(True, "OK"))

    def boom(key: str) -> tuple[bool, str]:
        raise KeyboardInterrupt

    p_second = Provider(
        env_var="B",
        name="ProvB",
        signup_url="https://x",
        instructions="step",
        validate=boom,
    )

    inputs = _Inputs(
        [
            "y",  # configure A
            "n",  # browser A
            "KEY_A",  # paste A
            "y",  # configure B
            "n",  # browser B
            "KEY_B",  # paste B (validate raises KeyboardInterrupt)
        ]
    )
    out = _Output()

    rc = run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(p_first, p_second),
    )

    assert rc == 1
    assert _read_env(env) == {"A": "KEY_A"}  # A saved before B interrupt
    assert "Interrupted" in out.text


def test_run_setup_validation_failure_does_not_save(tmp_path: Path):
    env = tmp_path / ".env"
    provider = _make_provider(validate_result=(False, "Invalid API key"))

    inputs = _Inputs(["y", "n", "BAD_KEY"])
    out = _Output()

    rc = run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(provider,),
    )

    assert rc == 0
    assert _read_env(env) == {}
    assert "Validation failed" in out.text
    assert "Invalid API key" in out.text


def test_run_setup_skip_provider_via_skip_keyword(tmp_path: Path):
    env = tmp_path / ".env"
    provider = _make_provider()

    inputs = _Inputs(["y", "n", "skip"])
    out = _Output()

    run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(provider,),
    )

    assert _read_env(env) == {}
    assert "no key provided" in out.text


def test_run_setup_existing_valid_key_is_kept(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("FAKE_KEY=already_good\n", encoding="utf-8")
    provider = _make_provider(validate_result=(True, "OK"))

    inputs = _Inputs([])  # no prompts expected
    out = _Output()

    run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(provider,),
    )

    assert _read_env(env) == {"FAKE_KEY": "already_good"}
    assert "kept existing" in out.text


def test_run_setup_existing_invalid_key_can_be_replaced(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("FAKE_KEY=stale\n", encoding="utf-8")

    # First validate call (existing key) returns False, second (new key) returns True.
    calls: list[str] = []

    def validate(key: str) -> tuple[bool, str]:
        calls.append(key)
        if key == "stale":
            return (False, "expired")
        return (True, "OK")

    provider = Provider(
        env_var="FAKE_KEY",
        name="Fake",
        signup_url="https://example.test",
        instructions="step 1",
        validate=validate,
    )

    inputs = _Inputs(
        [
            "y",  # replace existing invalid?
            "n",  # browser? skip
            "FRESH_KEY",  # paste new
        ]
    )
    out = _Output()

    run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(provider,),
    )

    assert _read_env(env) == {"FAKE_KEY": "FRESH_KEY"}
    assert calls == ["stale", "FRESH_KEY"]


def test_run_setup_existing_invalid_key_can_be_kept(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("FAKE_KEY=stale\n", encoding="utf-8")
    provider = _make_provider(validate_result=(False, "expired"))

    inputs = _Inputs(["n"])  # decline replacement
    out = _Output()

    run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(provider,),
    )

    assert _read_env(env) == {"FAKE_KEY": "stale"}
    assert "kept invalid key" in out.text


def test_run_setup_optional_provider_defaults_to_skip(tmp_path: Path):
    env = tmp_path / ".env"
    provider = _make_provider(optional=True)

    inputs = _Inputs([""])  # empty answer -> use default (no for optional)
    out = _Output()

    run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(provider,),
    )

    assert _read_env(env) == {}
    assert "user choice" in out.text


def test_run_setup_decline_to_configure_skips_cleanly(tmp_path: Path):
    env = tmp_path / ".env"
    provider = _make_provider()

    inputs = _Inputs(["n"])  # decline to configure at all
    out = _Output()

    rc = run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(provider,),
    )

    assert rc == 0
    assert _read_env(env) == {}
    assert "user choice" in out.text


def test_run_setup_browser_open_failure_is_swallowed(tmp_path: Path):
    env = tmp_path / ".env"
    provider = _make_provider()

    def boom(url: str) -> None:
        raise RuntimeError("no display")

    inputs = _Inputs(
        [
            "y",  # configure
            "y",  # open browser (will raise)
            "GOOD_KEY",  # paste
        ]
    )
    out = _Output()

    rc = run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=boom,
        env_path=env,
        providers=(provider,),
    )

    assert rc == 0
    assert _read_env(env) == {"FAKE_KEY": "GOOD_KEY"}


def test_run_setup_summary_lists_each_provider(tmp_path: Path):
    env = tmp_path / ".env"
    p_ok = _make_provider(env_var="A", name="ProvA", validate_result=(True, "OK"))
    p_skip = _make_provider(env_var="B", name="ProvB")
    p_fail = _make_provider(env_var="C", name="ProvC", validate_result=(False, "denied"))

    inputs = _Inputs(
        [
            "y",  # configure A
            "n",  # browser A
            "KEY_A",  # paste A
            "n",  # configure B -> skip
            "y",  # configure C
            "n",  # browser C
            "KEY_C",  # paste C (will fail validation)
        ]
    )
    out = _Output()

    run_setup(
        input_fn=inputs,
        output_fn=out,
        browser_fn=_no_browser,
        env_path=env,
        providers=(p_ok, p_skip, p_fail),
    )

    assert _read_env(env) == {"A": "KEY_A"}
    assert "ProvA" in out.text
    assert "ProvB" in out.text
    assert "ProvC" in out.text
    assert "saved" in out.text
    assert "user choice" in out.text
    assert "denied" in out.text


@pytest.mark.parametrize(
    "validator_name",
    [
        "_validate_ticketmaster",
        "_validate_eventbrite",
        "_validate_yelp",
        "_validate_nyc_opendata",
    ],
)
def test_validators_handle_network_error(monkeypatch, validator_name: str):
    """Each validator should return (False, msg) on httpx errors, not raise."""
    import httpx

    from touch_grass import setup_wizard

    def boom(*args, **kwargs):
        raise httpx.ConnectError("simulated")

    monkeypatch.setattr(setup_wizard.httpx, "get", boom)
    fn = getattr(setup_wizard, validator_name)
    ok, msg = fn("any_key")
    assert ok is False
    assert "Network error" in msg


@pytest.mark.parametrize(
    "validator_name,success_status,invalid_status",
    [
        ("_validate_ticketmaster", 200, 401),
        ("_validate_eventbrite", 200, 401),
        ("_validate_yelp", 200, 401),
        ("_validate_nyc_opendata", 200, 403),
    ],
)
def test_validators_decode_status_codes(
    monkeypatch, validator_name: str, success_status: int, invalid_status: int
):
    from touch_grass import setup_wizard

    class FakeResp:
        def __init__(self, code: int):
            self.status_code = code

    def make_get(code: int):
        def _get(*args, **kwargs):
            return FakeResp(code)

        return _get

    fn = getattr(setup_wizard, validator_name)

    monkeypatch.setattr(setup_wizard.httpx, "get", make_get(success_status))
    ok, _ = fn("k")
    assert ok is True

    monkeypatch.setattr(setup_wizard.httpx, "get", make_get(invalid_status))
    ok, msg = fn("k")
    assert ok is False
    assert msg  # non-empty message
