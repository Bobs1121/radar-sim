"""
AI Q&A plugin — answer questions about analysis results using LLM.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from core.analysis_runner import AnalysisPlugin
from core.models import AnalysisContext, PluginResult, SignalData

logger = logging.getLogger(__name__)


class AIQAPlugin(AnalysisPlugin):
    """Answer user questions about signal analysis results using LLM."""

    def __init__(self):
        self.client = None

    @property
    def name(self) -> str:
        return "ai_qa"

    def analyze(self, signals: dict[str, SignalData], context: AnalysisContext) -> PluginResult:
        """Run AI analysis on signal data."""
        ai_cfg = self._get_ai_config(context)
        if not ai_cfg or not ai_cfg.get("enabled", True):
            return PluginResult(
                plugin_name=self.name, success=False,
                summary="AI analysis disabled",
                errors=["AI not configured"],
            )

        # Build summary for AI
        signal_summaries = []
        for name, sig in signals.items():
            s = sig.summary or {}
            signal_summaries.append(
                f"- {name}: min={s.get('min', '?')}, max={s.get('max', '?')}, "
                f"mean={s.get('mean', '?')}, transitions={s.get('transitions', '?')}, "
                f"last={s.get('last', '?')}, unit={sig.unit}"
            )

        prompt = f"""You are a radar signal analysis assistant. Analyze the following simulation output signals and provide insights.

Signals from MF4 analysis:
{chr(10).join(signal_summaries)}

Project: {context.project}
Platform: {context.platform}

Please provide:
1. Key observations about the signal behavior
2. Any potential issues or anomalies
3. Recommendations for next steps
"""

        if context.user_context:
            prompt += f"\nUser context: {context.user_context}"

        try:
            client = self._get_client(ai_cfg)
            response = client.chat.completions.create(
                model=ai_cfg["model"],
                messages=[{"role": "user", "content": prompt}],
                temperature=ai_cfg.get("temperature", 0.1),
                max_tokens=ai_cfg.get("max_tokens", 4096),
                timeout=ai_cfg.get("timeout", 120),
            )

            # Handle qwen3 reasoning field
            content = response.choices[0].message.content or ""
            if not content and hasattr(response.choices[0].message, "reasoning"):
                content = response.choices[0].message.reasoning or ""

            return PluginResult(
                plugin_name=self.name,
                success=True,
                data={"analysis": content},
                summary="AI analysis complete",
            )

        except Exception as e:
            logger.error(f"AI analysis failed: {e}")
            return PluginResult(
                plugin_name=self.name,
                success=False,
                summary=f"AI analysis failed: {e}",
                errors=[str(e)],
            )

    def ask(self, question: str, signals: dict[str, SignalData], context: AnalysisContext) -> str:
        """Answer a specific question about the analysis."""
        ai_cfg = self._get_ai_config(context)
        if not ai_cfg or not ai_cfg.get("enabled", True):
            return "AI is not configured. Please set up analysis.ai in default.yaml."

        # Build signal context
        signal_summaries = []
        for name, sig in signals.items():
            s = sig.summary or {}
            signal_summaries.append(
                f"- {name}: min={s.get('min', '?')}, max={s.get('max', '?')}, "
                f"mean={s.get('mean', '?')}, transitions={s.get('transitions', '?')}, "
                f"last={s.get('last', '?')}, unit={sig.unit}"
            )

        prompt = f"""You are a radar signal analysis assistant. Here are the analyzed signals:

{chr(10).join(signal_summaries)}

Project: {context.project}

Please answer this question concisely:
{question}
"""

        try:
            client = self._get_client(ai_cfg)
            response = client.chat.completions.create(
                model=ai_cfg["model"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=2048,
                timeout=ai_cfg.get("timeout", 120),
            )

            content = response.choices[0].message.content or ""
            if not content and hasattr(response.choices[0].message, "reasoning"):
                content = response.choices[0].message.reasoning or ""

            return content or "AI returned empty response."

        except Exception as e:
            return f"AI query failed: {e}"

    def _get_ai_config(self, context: AnalysisContext) -> Optional[dict]:
        """Get AI configuration."""
        from core.config import load_global_defaults
        global_cfg = load_global_defaults()
        return global_cfg.get("analysis", {}).get("ai")

    def _get_client(self, ai_cfg: dict):
        """Get OpenAI-compatible client."""
        if self.client is None:
            try:
                from openai import OpenAI
                api_key = ai_cfg.get("api_key", "")
                if not api_key:
                    env_key = ai_cfg.get("api_key_env", "MODEL_FARM_API_KEY")
                    import os
                    api_key = os.environ.get(env_key, "dummy")

                self.client = OpenAI(
                    base_url=ai_cfg["base_url"],
                    api_key=api_key,
                )
            except ImportError:
                raise RuntimeError("openai package not installed. Run: pip install openai")

        return self.client
