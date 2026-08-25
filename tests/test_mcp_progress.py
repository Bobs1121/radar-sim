from __future__ import annotations

from io import StringIO

from radar_sim_mcp.progress import emit_progress, render_progress


def test_render_progress_is_reader_friendly_and_bounded() -> None:
    value = render_progress(62.5, label="仿真处理中", stage="collect_results", job_id="job_12345678901234567890")

    assert value.startswith("[radar-sim] 仿真处理中 [")
    assert "62.5%" in value
    assert "collect_results" in value
    assert "job_1234567890123" in value
    assert "█" in value and "░" in value


def test_emit_progress_uses_stderr_style_stream_only() -> None:
    stream = StringIO()

    emit_progress(100, label="仿真完成", stream=stream)

    assert stream.getvalue().startswith("[radar-sim] 仿真完成")
