from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    webhook_shared_secret: str = "dummy_dev_secret_change_me"
    anthropic_api_key: str = ""
    database_url: str = "sqlite:///./kairo.db"

    decision_matrix_path: str = "config/decision_matrix.yaml"
    engine_version: str = "0.1.0"
    matrix_version: str = "unset"  # stamped from the loaded YAML once M1 lands

    # Governance constants (recovery-decision-matrix.md, Part 4)
    global_max_retry_attempts: int = 3
    min_cooling_off_hours: int = 2
    max_contacts_per_cycle: int = 2
    recovery_cycle_days: int = 7
    high_value_threshold_inr: int = 5000

    # Resolution: confidence below this, or a reason code not in the matrix, -> B_UNKNOWN
    unknown_bucket_confidence_threshold: float = 0.5

    # NPCI restricted window (IST hours, 24h clock)
    npci_restricted_start_hour: int = 10
    npci_restricted_end_hour: int = 13
    npci_snap_hour: int = 13
    npci_snap_minute: int = 30


settings = Settings()
