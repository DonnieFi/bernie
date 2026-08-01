"""Presence network adapters (UniFi, HA WiFi scanner)."""

from presence.adapters import HANetworkPresenceAdapter, PresenceAdapter, UniFiPresenceAdapter

__all__ = ["PresenceAdapter", "UniFiPresenceAdapter", "HANetworkPresenceAdapter"]
