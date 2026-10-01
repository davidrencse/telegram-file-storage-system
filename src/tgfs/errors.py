import errno


class TgfsError(Exception):
    """Base error. `errno` lets the FUSE layer map failures to OS error codes."""

    errno = errno.EIO


class NotFound(TgfsError):
    errno = errno.ENOENT


class AlreadyExists(TgfsError):
    errno = errno.EEXIST


class NotADirectory(TgfsError):
    errno = errno.ENOTDIR


class IsADirectory(TgfsError):
    errno = errno.EISDIR


class DirectoryNotEmpty(TgfsError):
    errno = errno.ENOTEMPTY


class InvalidOperation(TgfsError):
    errno = errno.EINVAL


class IntegrityError(TgfsError):
    """Downloaded data does not match the recorded checksum, or decryption failed."""


class PassphraseRequired(TgfsError):
    errno = errno.EACCES
