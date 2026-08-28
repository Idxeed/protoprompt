"""Exceptions for the secrets subsystem."""


class SecretKeyError(RuntimeError):
    """A :class:`~protoprompt.secrets.key.KeyProvider` could not supply
    the master encryption key (missing env var, no key file, no backend).
    """
