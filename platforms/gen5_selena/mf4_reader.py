"""
Gen5 MF4 signal reader — asammdf-based signal extraction.

Extracts time series from Vector MDF4 output files.
Supports fuzzy matching for signal name lookup.
"""

from __future__ import annotations

from typing import Any, Optional


class Gen5Mf4Reader:
    """Extract signals from .mf4 output files using asammdf."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def extract(
        self, output_file: str, signal_names: list[str]
    ) -> dict[str, "SignalData"]:
        """Extract specified signals from MF4 file.

        Args:
            output_file: Path to output .mf4 file.
            signal_names: List of signal names to extract.

        Returns:
            Dict mapping signal name -> SignalData.
        """
        from asammdf import MDF
        from core.models import SignalData

        mdf = MDF(output_file)
        result: dict[str, SignalData] = {}

        available = list(mdf.channels_db.keys())

        for sig_name in signal_names:
            # Try exact match first
            matched_name = sig_name
            if sig_name not in available:
                # Fuzzy match
                matched_name = self._fuzzy_match(sig_name, available)
                if not matched_name:
                    continue

            try:
                sig = mdf.get(matched_name)
                result[matched_name] = SignalData(
                    name=matched_name,
                    timestamps=sig.timestamps.tolist(),
                    values=sig.values.tolist(),
                    unit=getattr(sig, "unit", "") or "",
                    source_mf4=output_file,
                )
            except Exception:
                # Signal exists but couldn't be read (e.g., complex type)
                pass

        mdf.close()
        return result

    def list_available_signals(self, mf4_path: str) -> list[str]:
        """List all available signal names in the MF4 file."""
        from asammdf import MDF

        mdf = MDF(mf4_path)
        signals = list(mdf.channels_db.keys())
        mdf.close()
        return signals

    @staticmethod
    def _fuzzy_match(target: str, available: list[str]) -> Optional[str]:
        """Find best fuzzy match for target among available signal names.

        Uses substring matching first, then case-insensitive comparison.
        Returns None if no match found.
        """
        # Case-insensitive exact match
        target_lower = target.lower()
        for name in available:
            if name.lower() == target_lower:
                return name

        # Substring match (target contained in signal name)
        for name in available:
            if target_lower in name.lower():
                return name

        # Signal name contained in target
        for name in available:
            if name.lower() in target_lower:
                return name

        # Partial token match
        best_score = 0
        best_match = None
        target_tokens = set(target_lower.split())
        for name in available:
            name_tokens = set(name.lower().split())
            score = len(target_tokens & name_tokens)
            if score > best_score:
                best_score = score
                best_match = name

        return best_match if best_score > 0 else None
