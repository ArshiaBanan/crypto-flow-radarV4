from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "dev"
    log_level: str = "INFO"

    lcw_api_key: str | None = None
    lcw_enabled: bool = True
    lcw_currency: str = "BTC"
    lcw_volume_min_btc: float = 200.0
    lcw_volume_max_btc: float = 500.0
    lcw_page_size: int = 100
    lcw_max_pages: int = 25
    lcw_refresh_seconds: int = 60

    cryptometer_api_key: str | None = None
    cryptometer_enabled: bool = True
    cryptometer_base_url: str = "https://api.cryptometer.io"
    cryptometer_refresh_seconds: int = 30

    binance_futures_ws_url: str = "wss://fstream.binance.com/stream"
    binance_futures_rest_url: str = "https://fapi.binance.com"
    binance_enabled: bool = True
    binance_symbol_refresh_seconds: int = 300
    binance_depth_levels: int = 20
    binance_ws_max_symbols: int = 80
    sqlite_path: str = "data/radar.db"
    data_engine_enabled: bool = True

    # v2.0 production controls
    storage_backend: str = "sqlite"
    admin_api_key: str | None = None
    v20_enabled: bool = True
    v20_alert_cooldown_seconds: int = 900
    v20_stop_pct: float = 1.5
    v20_target_pct: float = 4.0
    v20_horizon_min: int = 60
    v20_per_alert_risk_pct: float = 1.0
    v20_max_correlated_exposure_pct: float = 3.0
    v20_correlation_threshold: float = 0.75
    v20_auto_paper_submit: bool = True
    v20_telegram_enabled: bool = False
    v20_telegram_bot_token: str | None = None
    v20_telegram_chat_id: str | None = None
    # v2.1 validation / promotion controls
    v21_min_resolved: int = 100
    v21_min_expectancy_r: float = 0.10
    v21_expectancy_confidence: float = 0.95
    v21_bootstrap_iters: int = 2000
    v21_max_drawdown_pct: float = 20.0
    v21_max_ambiguous_rate: float = 0.10
    v21_max_no_data_rate: float = 0.05
    v21_min_positive_rate: float = 0.50
    v21_max_calibration_brier: float | None = 0.25
    # v2.2 observation ledger
    v22_observation_enabled: bool = True
    v22_code_version: str = "2.2.0"
    v22_environment: str = "paper"
    # v2.3 multi-source live enrichment
    v23_enrichment_enabled: bool = False
    v23_lcw_interval_seconds: int = 60
    v23_cryptometer_interval_seconds: int = 60
    v23_include_orderbook: bool = True
    # v2.4 external intelligence / source consensus
    v24_intelligence_enabled: bool = False
    v24_coinglass_api_key: str | None = None
    v24_coinglass_base_url: str = "https://open-api-v4.coinglass.com"
    v24_gecko_enabled: bool = True
    v24_gecko_base_url: str = "https://api.geckoterminal.com/api/v2"
    v24_gecko_networks: str = "solana"
    v24_goplus_api_key: str | None = None
    v24_goplus_enabled: bool = True
    v24_poll_seconds: int = 30
    v24_min_consensus_families: int = 2
    v24_min_consensus_agreement: float = 0.65
    v24_consensus_max_age_ms: int = 180000
    # v2.5 resilience / production hardening
    v25_enabled: bool = True
    v25_http_timeout_seconds: float = 15.0
    v25_http_max_attempts: int = 3
    v25_backoff_base_seconds: float = 0.5
    v25_backoff_max_seconds: float = 8.0
    v25_circuit_failure_threshold: int = 3
    v25_circuit_open_seconds: float = 30.0
    v25_max_response_bytes: int = 4_000_000
    v25_max_concurrency: int = 4
    v25_min_request_interval_ms: int = 0
    v25_fail_closed_external: bool = True
    # v3.0 production deployment / observability
    v30_enabled: bool = True
    postgres_url: str | None = None
    redis_url: str | None = None
    v30_require_postgres: bool = False
    v30_require_redis: bool = False
    v30_redis_lease_key: str = "crypto-flow-radar:runtime-lease"
    v30_redis_lease_ttl_seconds: int = 30
    v30_require_admin_key_in_prod: bool = True
    v30_max_heartbeat_age_seconds: int = 60
    # v3.2 full ledger dual-write / reconciliation
    v32_enabled: bool = False
    v32_dualwrite_mode: str = "shadow"  # off | shadow | required
    v32_poll_seconds: float = 2.0
    v32_batch_size: int = 500
    v32_max_pending_events: int = 100
    v32_max_pending_age_ms: int = 120_000
    v32_require_reconciliation: bool = False
    v32_tables: str = ""
    # v3.3 live paper validation / production integration
    v33_enabled: bool = True
    v33_validation_default_min_resolved: int = 100
    v33_validation_default_bootstrap_iters: int = 2000
    v33_runtime_manifest_enabled: bool = True
    # v3.5 SaaS identity / workspace
    v35_enabled: bool = True
    v35_session_ttl_seconds: int = 1209600
    v35_cookie_name: str = "cfr_session"
    v35_cookie_secure: bool = False
    v35_default_plan: str = "RESEARCH"
    # v3.6 SaaS identity / email verification / subscriptions
    v36_enabled: bool = True
    v36_session_ttl_seconds: int = 1209600
    v36_cookie_name: str = "cfr_session_v36"
    v36_cookie_secure: bool = False
    v36_smtp_host: str = "smtp.gmail.com"
    v36_smtp_port: int = 587
    v36_smtp_username: str | None = None
    v36_smtp_password: str | None = None
    v36_smtp_from_email: str | None = None
    v36_email_mode: str = "smtp"  # smtp | console
    v36_email_timeout_seconds: float = 15.0
    v36_billing_webhook_secret: str | None = None
    v36_require_verified_email: bool = True
    v36_telegram_bot_token: str | None = None
    v36_twilio_account_sid: str | None = None
    v36_twilio_auth_token: str | None = None
    v36_twilio_from_number: str | None = None
    # v3.7 multi-asset market/data federation
    v37_binance_spot_enabled: bool = True
    v37_binance_spot_base_url: str = "https://api.binance.com"
    v37_cmc_enabled: bool = True
    v37_cmc_api_key: str | None = None
    v37_cmc_base_url: str = "https://pro-api.coinmarketcap.com"
    v37_cmc_limit: int = 200
    v37_exchange_fanout_enabled: bool = True
    v37_oanda_enabled: bool = False
    v37_oanda_base_url: str = "https://api-fxtrade.oanda.com"
    v37_oanda_api_token: str | None = None
    v37_oanda_account_id: str | None = None
    v37_fxcm_enabled: bool = False
    v37_fxcm_base_url: str = "https://endpoints.fxcm.com"
    v37_fxcm_username: str | None = None
    v37_fxcm_password: str | None = None
    v37_fxcm_trading_session_id: str = "FXCM"
    v37_fxcm_trading_session_sub_id: str | None = None
    v37_arkham_api_key: str | None = None
    v37_arkham_base_url: str = "https://api.arkm.com"
    v37_etherscan_api_key: str | None = None
    v37_etherscan_base_url: str = "https://api.etherscan.io/v2/api"
    v37_refresh_seconds: int = 15
    # v3.8 extended macro / index / energy / dominance feeds
    v38_tradingeconomics_enabled: bool = False
    v38_tradingeconomics_api_key: str | None = None
    v38_tradingeconomics_base_url: str = "https://api.tradingeconomics.com"
    v38_oanda_us30_symbol: str = "US30_USD"
    v38_oanda_spx500_symbol: str = "SPX500_USD"
    v38_oanda_us100_symbol: str = "NAS100_USD"
    v38_oanda_usoil_symbol: str = "WTICO_USD"
    v38_oanda_ukoil_symbol: str = "BCO_USD"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()
