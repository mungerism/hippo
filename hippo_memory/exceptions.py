"""Exceptions for Hippo Memory Hub."""


class HippoError(Exception):
    """Base exception for Hippo memory hub."""


class HippoValidationError(HippoError, ValueError):
    """Raised when client input parameters fail validation.

    Inherits from ValueError for backwards compatibility, but provides a distinct
    type to differentiate user input validation errors from unexpected backend/driver ValueErrors.
    """


class HippoLockTimeoutError(HippoError, TimeoutError):
    """Raised when acquiring an identity or consolidation lock times out.

    Inherits from TimeoutError for backwards compatibility with existing callers
    and tests that catch TimeoutError, while providing rich diagnostic context.
    """

    def __init__(
        self,
        message: str,
        *,
        identity: tuple = (),
        lock_path: object = None,
        timeout: object = None,
    ):
        super().__init__(message)
        self.identity = identity
        self.lock_path = lock_path
        self.timeout = timeout

