from __future__ import annotations


class KiwiError(Exception):
    pass


class PlatformApiError(KiwiError):
    pass


class MessageTooLargeError(KiwiError):
    pass


class ScriptExecutionError(KiwiError):
    pass
