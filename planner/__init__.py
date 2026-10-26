"""Planner interface (plan §1.4): task + dossier context -> JSON plan.

``MockPlanner`` provides the deterministic plan used by the demo
scenario. ``SparkPlanner`` is the live planner: it asks Muse Spark
(Meta Model API) for a plan through the PC bridge (the credential never
leaves the PC), validates the reply fail-closed, and falls back to the
mock — with the reason audit-logged — on any failure. Selection is by
``PLANNER=spark|mock`` (default mock) via :func:`planner_from_env`.
"""

from .planner import MockPlanner, Planner, SparkPlanner, planner_from_env
from .transport import BridgeSparkTransport, SparkTransportError

__all__ = [
    "BridgeSparkTransport",
    "MockPlanner",
    "Planner",
    "SparkPlanner",
    "SparkTransportError",
    "planner_from_env",
]
