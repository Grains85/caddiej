"""GSPro Open Connect adapter.

GSPro's wire format lives in `wire.py` and nowhere else. Callers of this package
see CaddieJ concepts only: an adapter with a lifecycle, and domain events.
"""

from caddiej.adapters.gspro.adapter import GSProConnectAdapter, GSProHealth
from caddiej.adapters.gspro.errors import (
    GSProBindError,
    GSProConnectionError,
    GSProError,
    GSProProtocolError,
)
from caddiej.adapters.gspro.factory import build_adapter
from caddiej.adapters.gspro.round_file import (
    GSProRoundFileAdapter,
    GSProRoundObserverHealth,
    RoundObserverStatus,
)
from caddiej.adapters.gspro.simulation import run_simulated_round

__all__ = [
    "GSProBindError",
    "GSProConnectAdapter",
    "GSProConnectionError",
    "GSProError",
    "GSProHealth",
    "GSProProtocolError",
    "GSProRoundFileAdapter",
    "GSProRoundObserverHealth",
    "RoundObserverStatus",
    "build_adapter",
    "run_simulated_round",
]
