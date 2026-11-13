"""Custom exceptions for the interaction ledger and reporting pipeline."""


class PipelineError(Exception):
    """Base exception for all pipeline errors."""


class ConfigurationError(PipelineError):
    """Raised when application configuration or environment variables are invalid."""


class PIIConfigurationError(ConfigurationError):
    """Raised when PII encryption or HMAC keys are missing or invalid."""


class PIIDecryptionError(PipelineError):
    """Raised when ciphertext authentication or decryption fails."""


class SchemaLoadError(PipelineError):
    """Raised when a workflow schema cannot be found or parsed."""


class IngestionError(PipelineError):
    """Raised when data ingestion encounters an unrecoverable failure."""
