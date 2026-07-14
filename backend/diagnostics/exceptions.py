class DiagnosticsError(Exception):
    """Base class for all diagnostics bundle failures."""


class DiagnosticsUnsupported(DiagnosticsError):
    """Raised when no adapter exists for the server's vendor."""


class DiagnosticsTimeout(DiagnosticsError):
    """Raised when a job doesn't complete within the allotted time."""