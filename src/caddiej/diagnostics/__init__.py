"""Session diagnostics for one round of swing coaching.

Reads facts the capture, evidence, and coaching path already produce and
writes a report after the fact. It does not rank checkpoints, choose frames,
or speak.
"""

from caddiej.diagnostics.export import export_session
from caddiej.diagnostics.journal import DiagnosticJournal

__all__ = ["DiagnosticJournal", "export_session"]
