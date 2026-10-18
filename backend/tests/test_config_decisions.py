from pydantic import SecretStr

from app.config import Settings


def test_gemini_compatible_provider_is_the_configurable_default() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://localhost/incidentpilot",
        redis_url="redis://localhost:6379/0",
        jwt_secret=SecretStr("test-only-secret"),
    )

    assert settings.llm_base_url == (
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    assert settings.llm_model == "gemini-3.5-flash-lite"


def test_llm_model_can_be_selected_through_environment(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "gemini-test-model")
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://localhost/incidentpilot",
        redis_url="redis://localhost:6379/0",
        jwt_secret=SecretStr("test-only-secret"),
    )

    assert settings.llm_model == "gemini-test-model"
