from core.business_progress import business_steps


def _stage(stage_type: str, status: str, progress: float = 0.0, error=None):
    return {
        "stage_type": stage_type,
        "status": status,
        "progress": progress,
        "error": error or {},
    }


def test_business_steps_project_fixed_dag_to_four_user_steps():
    stages = [
        _stage("resolve_spec", "succeeded"),
        _stage("environment_check", "succeeded"),
        _stage("prepare_source", "skipped"),
        _stage("build_selena", "running", 0.5),
        _stage("register_artifact", "queued"),
        _stage("prepare_data", "succeeded"),
        _stage("preflight", "queued"),
        _stage("run_simulation", "queued"),
        _stage("collect_results", "queued"),
        _stage("finalize_manifest", "queued"),
    ]

    projected = business_steps(stages)

    assert [item["id"] for item in projected] == [
        "resolve_inputs",
        "prepare_execution",
        "run_simulation",
        "collect_results",
    ]
    assert projected[0]["status"] == "succeeded"
    assert projected[1]["status"] == "running"
    assert projected[1]["progress"] == 0.5


def test_business_steps_surface_first_failed_or_blocked_error_without_paths():
    projected = business_steps(
        [
            _stage("preflight", "failed", error={"code": "INPUT_UNAVAILABLE", "message": "input unavailable"}),
            _stage("run_simulation", "cancelled"),
        ]
    )

    assert projected == [
        {
            "id": "run_simulation",
            "label": "执行仿真",
            "status": "failed",
            "progress": 0.0,
            "stage_types": ["preflight", "run_simulation"],
            "error": {"code": "INPUT_UNAVAILABLE", "message": "input unavailable"},
        }
    ]


def test_business_steps_treat_skipped_internal_work_as_completed_progress():
    projected = business_steps(
        [
            _stage("build_selena", "skipped"),
            _stage("register_artifact", "skipped"),
            _stage("prepare_data", "succeeded"),
        ]
    )

    assert projected[0]["status"] == "succeeded"
    assert projected[0]["progress"] == 1.0
