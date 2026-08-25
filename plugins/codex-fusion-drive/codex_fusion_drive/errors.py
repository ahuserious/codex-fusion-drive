"""Domain errors returned through the MCP boundary."""


class FusionDriveError(Exception):
    """Base error for fail-closed workflow failures."""


class ConfigurationError(FusionDriveError):
    """The requested configuration is invalid or stale."""


class LifecycleError(FusionDriveError):
    """A lifecycle transition or receipt is invalid."""


class CapabilityError(FusionDriveError):
    """A requested provider or local capability is unavailable."""


class ExternalActionRequired(FusionDriveError):
    """The host or user must explicitly approve an external action."""


class LockTimeout(FusionDriveError):
    """A runtime lock was held past the caller's deadline."""

