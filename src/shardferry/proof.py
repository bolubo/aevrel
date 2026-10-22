"""Compatibility shim for the old flat proof module."""

from .verification.proof import (
    ProofError,
    build_proof,
    combine_proofs,
    validate_proof,
    verify_proof,
)

__all__ = [
    "ProofError",
    "build_proof",
    "combine_proofs",
    "validate_proof",
    "verify_proof",
]
