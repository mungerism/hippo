"""Exceptions for Hippo Memory Hub."""


class HippoError(Exception):
    """Base exception for Hippo memory hub."""


class HippoValidationError(HippoError, ValueError):
    """Raised when client input parameters fail validation.

    Inherits from ValueError for backwards compatibility, but provides a distinct
    type to differentiate user input validation errors from unexpected backend/driver ValueErrors.
    """
