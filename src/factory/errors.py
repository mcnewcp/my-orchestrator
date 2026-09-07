"""Errors that leave a run durably blocked, with an operator-readable reason."""


class FactoryError(Exception):
    """A rejected request or stage failure."""


class Blocked(FactoryError):
    """A workflow cannot continue without operator intervention."""


class Interrupted(FactoryError):
    """The worker is shutting down or its database lease was lost."""
