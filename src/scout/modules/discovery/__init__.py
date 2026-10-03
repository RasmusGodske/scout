"""Domain to careers page, board and jobs. See README.md.

Public surface: `Resolver`, `DiscoveryConfig`, `Known`, `normalize_domain`.
"""

from scout.modules.discovery._resolver import DiscoveryConfig, Known, Resolver, normalize_domain

__all__ = ["DiscoveryConfig", "Known", "Resolver", "normalize_domain"]
