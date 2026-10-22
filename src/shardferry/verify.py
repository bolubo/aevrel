"""Compatibility shim for the old flat verification module."""

from .verification.verify import VerificationError, verify

__all__ = ["VerificationError", "verify"]
