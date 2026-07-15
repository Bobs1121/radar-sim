import multiprocessing

import pytest

from core.build_diagnostics import classify_build_failure, extract_actionable_build_errors
from core.build_lock import BuildLockError, WorkspaceBuildLock


def test_real_msbuild_error_is_prioritized_over_r2d2_wrapper():
    lines = [
        "warning C4577: noexcept used with no exception handling mode specified",
        r"runtime.cpp(20): error C2382: Runtime::~Runtime redefinition [fw.vcxproj]",
        "Failed to run make (cmake --build ...)",
        "R2D2 execution failed with exit_code 1!",
        "Could not find logfile_r2d2.txt",
    ]
    errors = extract_actionable_build_errors(lines)
    assert "error C2382" in errors[0]
    assert errors[-1].startswith("R2D2 execution failed")
    assert all("Could not find logfile" not in item for item in errors)


def test_real_c2382_is_classified_as_source_not_environment():
    diagnostic = classify_build_failure(
        [
            "VS2019 not found",
            r"runtime.cpp(20): error C2382: 'rbDsp::Runtime::~Runtime': redefinition; different exception specifications",
            "R2D2 execution failed with exit_code 1!",
        ]
    )
    assert diagnostic.code == "SOURCE_EXCEPTION_SPEC_MISMATCH"
    assert diagnostic.category == "source"
    assert "runtime.cpp(20)" in diagnostic.detail
    assert "environment" not in diagnostic.summary.lower()


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("foo.cpp(3): fatal error C1083: Cannot open include file: 'x.h'", "SOURCE_OR_INCLUDE_DEPENDENCY_MISSING"),
        ("LINK : fatal error LNK1104: cannot open file 'abc.lib'", "LINK_LIBRARY_MISSING"),
        ("LINK : error LNK2019: unresolved external symbol foo", "LINK_FAILED"),
        ("error MSB8020: The build tools cannot be found", "TOOLCHAIN_UNAVAILABLE"),
    ],
)
def test_build_failure_codes_are_stable(line, code):
    assert classify_build_failure([line]).code == code


def _try_lock(path, queue):
    try:
        with WorkspaceBuildLock(path):
            queue.put("acquired")
    except BuildLockError:
        queue.put("blocked")


def test_workspace_build_lock_blocks_a_second_process(tmp_path):
    queue = multiprocessing.Queue()
    with WorkspaceBuildLock(tmp_path):
        process = multiprocessing.Process(target=_try_lock, args=(str(tmp_path), queue))
        process.start()
        process.join(timeout=5)
        assert queue.get(timeout=2) == "blocked"
    process = multiprocessing.Process(target=_try_lock, args=(str(tmp_path), queue))
    process.start()
    process.join(timeout=5)
    assert queue.get(timeout=2) == "acquired"
