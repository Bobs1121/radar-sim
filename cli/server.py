"""rsim server - minimal control-plane shell and HTTP server."""

from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from pathlib import Path

from core.control_http import make_control_handler
from core.control_service import ControlService, default_control_db_path

# This command runs on the control server (possibly Linux) without project config.
NO_CONFIG = True


def register(subparsers):
    parser = subparsers.add_parser("server", help="Run or inspect the minimal control server")
    server_sub = parser.add_subparsers(dest="server_command", help="Server commands")

    serve = server_sub.add_parser("serve", help="Start the stdlib HTTP JSON control server (legacy /api/* Agent endpoints)")
    serve.add_argument("--host", default="127.0.0.1", help="Bind host")
    serve.add_argument("--port", type=int, default=8877, help="Bind port")
    serve.add_argument("--db-path", default="", help="SQLite database path")
    serve.add_argument(
        "--allowed-task-types",
        default="",
        help="Comma-separated task_type whitelist (e.g. 'cluster.run'). Empty (default) "
        "accepts all task types — use this for Mode B (full local+cluster) servers. "
        "Set to 'cluster.run' for Mode A (Linux cluster-only service) so the server "
        "rejects local.check / local.build_selena / local.run_sim jobs with HTTP 400.",
    )
    serve.add_argument(
        "--cluster-executor",
        action="store_true",
        help="Retired compatibility flag. Project/profile Cluster execution is disabled; "
        "use `rsim server serve-v1` and its project-free Cluster Stage executor.",
    )

    serve_v1 = server_sub.add_parser(
        "serve-v1",
        help="Start the unified FastAPI /api/v1 and Windows Agent control server",
    )
    serve_v1.add_argument("--host", default="127.0.0.1", help="Bind host")
    serve_v1.add_argument("--port", type=int, default=8878, help="Bind port")
    serve_v1.add_argument("--db-path", default="", help="SQLite database path")
    serve_v1.add_argument(
        "--auth-file",
        default="",
        help="Versioned JSON Bearer credential file (required for non-loopback release binds)",
    )
    serve_v1.add_argument(
        "--insecure-no-auth",
        action="store_true",
        help="Explicitly allow an unauthenticated non-loopback bind (development only)",
    )
    serve_v1.add_argument(
        "--no-cluster-executor",
        action="store_true",
        help="Disable the built-in Linux/Gateway v2 Cluster Stage executor.",
    )

    create = server_sub.add_parser("create-job", help="Create a control job in the local control DB")
    create.add_argument("job_type", help="Job type, e.g. local.check or local.run_sim")
    create.add_argument("--db-path", default="", help="SQLite database path")
    create.add_argument("--project", default="", help="Project name to put in the job payload")
    create.add_argument("--config-path", default="", help="Config path to map to rsim --config")
    create.add_argument("--payload-json", default="", help="Extra JSON payload to merge into the task payload")
    create.add_argument("--metadata-json", default="", help="Optional JSON metadata for the job")
    create.add_argument("--input-mf4", default="", help="Input MF4 path for local.run_sim or cluster.run")
    create.add_argument("--input-path", default="", help="Generic input path alias for local.run_sim or cluster.run")
    create.add_argument("--dataset", default="", help="Dataset name for local.run_sim or cluster.run")
    create.add_argument("--profile", default="", help="Simulation or cluster profile")
    create.add_argument("--backend", default="", help="Backend override for local.check")
    create.add_argument("--output-mf4", default="", help="Explicit output MF4 path for local.run_sim")
    create.add_argument("--mode", default="", help="Build mode for local.build_selena")
    create.add_argument("--run-id", default="", help="Stable run id for cluster.run")
    create.add_argument("--timeout", type=int, default=0, help="Timeout in seconds for local.run_sim")
    create.add_argument("--limit", type=int, default=0, help="Selection limit for local.run_sim or cluster.run")
    create.add_argument("--max-duration", type=int, default=0, help="Per-file hard runtime limit for local.run_sim")
    create.add_argument("--stall-timeout", type=int, default=0, help="Per-file inactivity timeout for local.run_sim")
    create.add_argument("--max-minutes", type=int, default=0, help="Wait timeout in minutes for cluster.run")
    create.add_argument("--clean", action="store_true", help="Pass --clean to local.build_selena")
    create.add_argument("--deps", action="store_true", help="Pass --deps to local.check")
    create.add_argument("--dry-run", action="store_true", help="Pass --dry-run to local.run_sim")
    create.add_argument("--execute", action="store_true", help="Pass --execute to cluster.run")
    create.add_argument("--copy-data", action="store_true", help="Pass --copy-data to cluster.run")
    create.add_argument("--copy-selena", action="store_true", help="Pass --copy-selena to cluster.run")
    create.add_argument("--no-progress", action="store_true", help="Pass --no-progress to local.build_selena")
    create.add_argument("--select", action="store_true", help="Pass --select to local.run_sim or cluster.run")
    create.add_argument("--no-retry", action="store_true", help="Pass --no-retry to local.run_sim")
    create.add_argument("--no-wait", action="store_true", help="Pass --no-wait to cluster.run")
    create.add_argument("--no-fetch", action="store_true", help="Pass --no-fetch to cluster.run")
    create.add_argument("--required-signal", action="append", default=[], help="Repeatable signal filter for cluster.run")
    create.add_argument("--extra-arg", action="append", default=[], help="Repeatable extra arg for local.run_sim")

    get_job = server_sub.add_parser("get-job", help="Show a control job from the local control DB")
    get_job.add_argument("job_id", help="Job id")
    get_job.add_argument("--db-path", default="", help="SQLite database path")

    get_logs = server_sub.add_parser("get-logs", help="Show control task logs from the local control DB")
    get_logs.add_argument("job_id", help="Job id")
    get_logs.add_argument("--task-id", default="", help="Optional task id")
    get_logs.add_argument("--since", type=int, default=0, help="Cursor from previous read")
    get_logs.add_argument("--limit", type=int, default=200, help="Max log lines")
    get_logs.add_argument("--db-path", default="", help="SQLite database path")

    cancel = server_sub.add_parser("cancel", help="Cancel a control job in the local control DB")
    cancel.add_argument("job_id", help="Job id")
    cancel.add_argument("--db-path", default="", help="SQLite database path")

    reclaim = server_sub.add_parser(
        "reclaim",
        help="Requeue running tasks whose agent has gone silent (dead-agent recovery)",
    )
    reclaim.add_argument(
        "--stale-after", type=float, default=300.0,
        help="Seconds since last agent heartbeat before a task is considered stale (default 300)",
    )
    reclaim.add_argument(
        "--max-attempts", type=int, default=3,
        help="Fail tasks that have already been reclaimed this many times (default 3, 0=unlimited)",
    )
    reclaim.add_argument("--db-path", default="", help="SQLite database path")

    list_agents = server_sub.add_parser(
        "list-agents",
        help="List registered agents (id, status, last heartbeat, current task)",
    )
    list_agents.add_argument("--db-path", default="", help="SQLite database path")


def run(args, config):
    command = getattr(args, "server_command", "") or ""
    if command == "serve":
        return _run_serve(args)
    if command == "serve-v1":
        return _run_serve_v1(args)
    if command == "create-job":
        return _run_create_job(args)
    if command == "get-job":
        return _print_json(_service_from_args(args).get_job(args.job_id))
    if command == "get-logs":
        return _print_json(
            _service_from_args(args).get_logs(
                job_id=args.job_id,
                task_id=getattr(args, "task_id", "") or "",
                since=int(getattr(args, "since", 0) or 0),
                limit=int(getattr(args, "limit", 200) or 200),
            )
        )
    if command == "cancel":
        return _print_json(_service_from_args(args).cancel_job(args.job_id))
    if command == "reclaim":
        max_attempts = int(getattr(args, "max_attempts", 3) or 0)
        reclaimed = _service_from_args(args).reclaim_stale_tasks(
            stale_after_seconds=float(getattr(args, "stale_after", 300.0) or 300.0),
            max_attempts=(max_attempts if max_attempts > 0 else None),
        )
        return _print_json({"reclaimed": reclaimed, "count": len(reclaimed)})
    if command == "list-agents":
        agents = _service_from_args(args).list_agents()
        return _print_json({"agents": agents})
    print("Missing server command. Use: rsim server serve|serve-v1|create-job|get-job|get-logs|cancel|reclaim|list-agents")
    return 1


def _run_serve(args) -> int:
    host = getattr(args, "host", "127.0.0.1")
    port = int(getattr(args, "port", 8877))

    allowed_raw = getattr(args, "allowed_task_types", "") or ""
    allowed = {part.strip() for part in allowed_raw.split(",") if part.strip()} or None

    explicit_db = getattr(args, "db_path", "") or ""
    if explicit_db:
        # Single explicit DB → single-user mode (backward compatible).
        service = ControlService(Path(explicit_db))
        handler = make_control_handler(service, allowed_task_types=allowed)
        db_desc = str(explicit_db)
    else:
        # Multi-user: route to a per-user DB via the X-Rsim-User header.
        handler = make_control_handler(_per_user_service_factory(), allowed_task_types=allowed)
        db_desc = "per-user (RSIM_HOME/results/_control_<user>.db)"

    server = ThreadingHTTPServer((host, port), handler)
    print(f"Radar Sim control server: http://{host}:{port}/")
    print(f"Control DB: {db_desc}")
    if allowed:
        print(f"Allowed task types: {', '.join(sorted(allowed))}")
    else:
        print("Allowed task types: all (Mode B — full local+cluster)")

    executor = None
    if getattr(args, "cluster_executor", False):
        print(
            "The legacy project/profile Cluster executor is disabled. "
            "Use `rsim server serve-v1`; its Cluster Stage executor consumes "
            "the same project-free YAML/SDK contract."
        )
        server.server_close()
        return 2

    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    finally:
        if executor is not None:
            executor.stop()
        server.server_close()
    return 0


def _run_serve_v1(args) -> int:
    host = getattr(args, "host", "127.0.0.1")
    port = int(getattr(args, "port", 8878))
    explicit_db = getattr(args, "db_path", "") or ""
    auth_file = str(getattr(args, "auth_file", "") or "").strip()
    authenticator = None
    if auth_file:
        try:
            from core.http_auth import HttpAuthError, load_http_auth
            authenticator = load_http_auth(auth_file)
        except (HttpAuthError, OSError) as exc:
            print(f"Invalid HTTP authentication configuration: {exc}")
            return 2
    elif not _is_loopback_bind(host) and not bool(getattr(args, "insecure_no_auth", False)):
        print(
            "Refusing unauthenticated non-loopback serve-v1 bind. "
            "Provide --auth-file or explicitly use --insecure-no-auth for development."
        )
        return 2

    try:
        import importlib
        from core.api_v1 import ApiV1Service
        from core.api_v1_fastapi import create_app
        from core.artifact_store import ArtifactStore, default_artifact_catalog_db_path
        from core.artifact_upload_service import ArtifactUploadService, trusted_build_evidence_from_control
        from core.artifacts import ArtifactCatalog
        from core.dataset_store import DatasetStore, default_dataset_catalog_db_path, default_dataset_root
        from core.dataset_upload_service import DatasetUploadService, trusted_data_stage_evidence_from_control
        from core.datasets import DatasetCatalog
        from core.datasets import resolve_data_reference
        from core.shared_namespace import SharedNamespaceRegistry
        from core.source_resolution_runtime import build_legacy_source_resolution_inputs
        from core.runtime_bundle_catalog import RuntimeBundleCatalog
        from core.runtime_bundle_upload_service import RuntimeBundleUploadService, trusted_runtime_bundle_evidence_from_control
        from core.config_assets import ConfigAssetStore
        from core.local_results import default_result_catalog
        from core.result_upload_service import ResultUploadService
        from core.transfer_service import TransferError, TransferService, TransferStore
        from core.config import load_cluster_execution_config
        uvicorn = importlib.import_module("uvicorn")
    except ImportError as exc:
        print(
            "serve-v1 requires optional dependencies. Install with "
            "`pip install .[v5-server]` on Python 3.10+."
        )
        print(f"Missing dependency: {exc.name or exc}")
        return 2

    if explicit_db:
        db_path = Path(explicit_db)
        service = ControlService(db_path)

        def factory(user: str) -> ControlService:
            return service

        shared_catalog = ArtifactCatalog(db_path)
        artifact_store = ArtifactStore(
            root=db_path.parent / f"{db_path.stem}_artifacts",
            db_path=db_path,
        )
        dataset_catalog = DatasetCatalog(db_path)
        dataset_store = DatasetStore(
            root=db_path.parent / f"{db_path.stem}_datasets",
            db_path=db_path,
        )
        runtime_bundle_db = db_path.parent / f"{db_path.stem}_runtime_bundles.db"
        runtime_bundle_catalog = RuntimeBundleCatalog(runtime_bundle_db)
        runtime_bundle_store = ArtifactStore(
            root=db_path.parent / f"{db_path.stem}_runtime_bundles",
            db_path=runtime_bundle_db,
            object_filename="runtime-bundle.zip",
            storage_ref_prefix="shared://selena-bundles/",
        )
        config_asset_store = ConfigAssetStore(
            db_path.parent / f"{db_path.stem}_config_assets",
            db_path.parent / f"{db_path.stem}_config_assets.db",
        )
    else:
        # v2 uses one central control DB with owner-scoped rows.  A shared DB is
        # required so the Linux/Gateway executor can schedule every user's job
        # without discovering per-user database files from filesystem names.
        db_path = default_artifact_catalog_db_path().parent / "control_v1.db"
        service = ControlService(db_path)

        def factory(_user: str) -> ControlService:
            return service

        shared_catalog = ArtifactCatalog(default_artifact_catalog_db_path())
        artifact_store = ArtifactStore()
        dataset_catalog = DatasetCatalog(default_dataset_catalog_db_path())
        dataset_store = DatasetStore(default_dataset_root())
        runtime_bundle_db = default_artifact_catalog_db_path().parent / "runtime_bundles.db"
        runtime_bundle_catalog = RuntimeBundleCatalog(runtime_bundle_db)
        runtime_bundle_store = ArtifactStore(
            root=default_artifact_catalog_db_path().parent / "runtime_bundles",
            db_path=runtime_bundle_db,
            object_filename="runtime-bundle.zip",
            storage_ref_prefix="shared://selena-bundles/",
        )
        config_asset_store = ConfigAssetStore(
            default_artifact_catalog_db_path().parent / "config_assets",
            default_artifact_catalog_db_path().parent / "config_assets.db",
        )

    def catalog_factory(_user: str) -> ArtifactCatalog:
        return shared_catalog

    def evidence_provider(owner: str, evidence_ref: str):
        return trusted_build_evidence_from_control(factory(owner), owner, evidence_ref)

    artifact_upload_service = ArtifactUploadService(artifact_store, shared_catalog, evidence_provider)

    def artifact_upload_service_factory(_owner: str) -> ArtifactUploadService:
        return artifact_upload_service

    runtime_bundle_upload_service = RuntimeBundleUploadService(
        runtime_bundle_store,
        runtime_bundle_catalog,
        lambda owner, evidence_ref: trusted_runtime_bundle_evidence_from_control(
            factory(owner), owner, evidence_ref
        ),
    )

    def runtime_bundle_upload_service_factory(_owner: str) -> RuntimeBundleUploadService:
        return runtime_bundle_upload_service

    dataset_upload_service = DatasetUploadService(
        dataset_store,
        dataset_catalog,
        evidence_provider=lambda owner, evidence_ref: trusted_data_stage_evidence_from_control(
            factory(owner), owner, evidence_ref
        ),
    )

    def dataset_upload_service_factory(_owner: str) -> DatasetUploadService:
        return dataset_upload_service

    def config_loader(project: str, profile: str, data_path: str):
        from core.config import load_simulation_spec_bundle
        return load_simulation_spec_bundle(project, profile=profile, data_path=data_path)

    def source_resolution_provider(owner: str, spec):
        return build_legacy_source_resolution_inputs(
            owner,
            spec,
            catalog_factory=catalog_factory,
            config_loader=config_loader,
            now_fn=__import__("time").time,
            inspect_local_workspace=False,
        )

    def data_resolution_provider(owner: str, spec):
        from core.config import load_cluster_execution_config
        infrastructure_config = load_cluster_execution_config(spec.project)
        return resolve_data_reference(
            dataset_catalog,
            SharedNamespaceRegistry.from_config(infrastructure_config),
            owner=owner,
            project=spec.project,
            data_path=spec.data.path,
            required_signals=spec.data.required_signals,
        )

    def cluster_result_roots() -> list[Path]:
        """Return deployment-authorized Cluster workspaces for result archiving."""
        return _deployment_cluster_result_roots()

    result_catalog = default_result_catalog(
        extra_allowed_source_roots=cluster_result_roots()
    )
    # Windows-full local runs create their immutable ZIP on the Agent. Keep a
    # resumable upload store beside the central result catalog so Web/SDK can
    # download the same result regardless of where Selena executed.
    result_upload_store = ArtifactStore(
        root=result_catalog.storage_root,
        db_path=result_catalog.storage_root / ".store" / "result_uploads.db",
        object_filename="result.zip",
        storage_ref_prefix="shared://results/",
    )
    result_upload_service = ResultUploadService(result_upload_store, result_catalog)

    def result_upload_service_factory(_owner: str) -> ResultUploadService:
        return result_upload_service

    # Direct-transfer roots are deployment authority.  Keep them out of the
    # user YAML and construct one metadata-only TransferService for the shared
    # Linux control DB.  A missing/invalid root is represented by an empty
    # service so API submissions fail closed with the stable
    # ``cluster_direct_transfer_unavailable`` action rather than selecting a
    # legacy HTTP upload path.
    transfer_db = db_path.parent / f"{db_path.stem}_transfers.db"
    try:
        cluster_deployment = dict(load_cluster_execution_config("run-config-v2").get("cluster") or {})
        direct_deployment = dict(cluster_deployment.get("direct_transfer") or {})
        transfer_service = TransferService(
            TransferStore(transfer_db),
            client_target_root=direct_deployment.get("client_target_root")
            or cluster_deployment.get("client_target_root")
            or cluster_deployment.get("direct_transfer_root")
            or None,
            server_probe_root=direct_deployment.get("server_probe_root")
            or cluster_deployment.get("server_probe_root")
            or cluster_deployment.get("probe_root"),
        )
    except (OSError, TypeError, ValueError, TransferError) as exc:
        print(f"Direct-transfer deployment configuration unavailable: {exc}")
        transfer_service = TransferService(TransferStore(transfer_db))

    api_service = ApiV1Service(
        control_service_factory=factory,
        source_resolution_provider=source_resolution_provider,
        data_resolution_provider=data_resolution_provider,
        artifact_upload_service_factory=artifact_upload_service_factory,
        dataset_upload_service_factory=dataset_upload_service_factory,
        runtime_bundle_upload_service_factory=runtime_bundle_upload_service_factory,
        config_asset_store=config_asset_store,
        result_catalog=result_catalog,
        result_upload_service_factory=result_upload_service_factory,
        transfer_service=transfer_service,
        project_names_provider=lambda: __import__("core.config", fromlist=["list_projects"]).list_projects(),
    )
    app_kwargs = {"api_service": api_service}
    if authenticator is not None:
        app_kwargs["authenticator"] = authenticator
    app = create_app(**app_kwargs)
    cluster_stage_executor = None
    if not bool(getattr(args, "no_cluster_executor", False)):
        from core.cluster_runs import ClusterRunStore
        from core.cluster_stage_executor import ClusterStageContext, ClusterStageExecutor

        def resolve_transfer_storage_ref(
            storage_ref: str,
            *,
            owner: str,
            expected_size: int = 0,
            require_exists: bool = True,
        ) -> Path:
            """Resolve one owner-bound logical ref with metadata-only checks."""

            resolved = transfer_service.resolve_storage_ref(
                storage_ref,
                owner=owner,
                require_exists=require_exists,
            )
            if expected_size and int(resolved.stat().st_size) != int(expected_size):
                raise ValueError("resolved storage object size does not match manifest")
            return resolved

        cluster_stage_context = ClusterStageContext(
            runtime_catalog=runtime_bundle_catalog,
            runtime_store=runtime_bundle_store,
            dataset_catalog=dataset_catalog,
            config_assets=config_asset_store,
            run_store=ClusterRunStore(runtime_bundle_db.parent / "cluster_runs.db"),
            work_root=runtime_bundle_db.parent / "cluster_stage_work",
            config_loader=lambda project: __import__(
                "core.config", fromlist=["load_cluster_execution_config"]
            ).load_cluster_execution_config(project),
            result_catalog=result_catalog,
            server_probe_root=transfer_service.server_probe_root,
            storage_ref_resolver=resolve_transfer_storage_ref,
        )
        cluster_stage_executor = ClusterStageExecutor(service, cluster_stage_context)
        cluster_stage_executor.start()
    print(f"Radar Sim v1 API server: http://{host}:{port}/api/v1/")
    print("HTTP Bearer authentication: " + ("enabled" if authenticator is not None else "disabled (loopback/development)"))
    print("Linux/Gateway Cluster Stage executor: " + ("enabled" if cluster_stage_executor else "disabled"))
    print("Windows Agent control endpoints: enabled on this same server")
    try:
        uvicorn.run(app, host=host, port=port, workers=1)
    finally:
        if cluster_stage_executor is not None:
            cluster_stage_executor.stop()
    return 0


def _is_loopback_bind(host: str) -> bool:
    text = str(host or "").strip().lower()
    if text == "localhost":
        return True
    try:
        return __import__("ipaddress").ip_address(text).is_loopback
    except ValueError:
        return False


def _deployment_cluster_result_roots() -> list[Path]:
    """Resolve the generic Cluster workspace into a controlled local root."""
    from core.cluster import get_cluster_config
    from core.config import load_cluster_execution_config

    try:
        # get_cluster_config injects deployment-independent Cluster defaults
        # such as workspace_root before linux_mount_map translates them.
        cluster = get_cluster_config(
            load_cluster_execution_config("run-config-v2")
        )
        workspace = str(cluster.get("workspace_root") or "").strip()
        for unc_prefix, mount in dict(cluster.get("linux_mount_map") or {}).items():
            if workspace.lower().startswith(str(unc_prefix).lower()):
                workspace = str(mount) + workspace[len(str(unc_prefix)):].replace("\\", "/")
                break
        root = Path(workspace).expanduser()
        return [root] if workspace and root.is_dir() else []
    except Exception:
        return []


def _start_cluster_executor(host: str, port: int):
    """Reject the retired project/profile Cluster execution path."""
    raise RuntimeError(
        "legacy project/profile Cluster execution is disabled; use `rsim server serve-v1`"
    )


def _per_user_service_factory():
    """Return a ``(user) -> ControlService`` that caches one service per user."""
    from core.user import control_db_path_for_user
    cache: dict[str, ControlService] = {}
    lock = __import__("threading").Lock()

    def factory(user: str) -> ControlService:
        with lock:
            if user not in cache:
                cache[user] = ControlService(control_db_path_for_user(user))
            return cache[user]

    return factory


def _run_create_job(args) -> int:
    payload = _parse_json_flag(getattr(args, "payload_json", "") or "", "payload")
    metadata = _parse_json_flag(getattr(args, "metadata_json", "") or "", "metadata")
    # Start from the explicit JSON payload so values like "project" survive even
    # when the matching CLI flag is at its empty default. CLI flags then layer on
    # top, but only when actually set (non-empty) — otherwise the create-job
    # subcommand's own --project default ("") would clobber a project passed via
    # --payload-json or via the global rsim --project.
    task_payload = dict(payload)
    cli_overrides = {
        "project": getattr(args, "project", "") or "",
        "config_path": getattr(args, "config_path", "") or "",
        "profile": getattr(args, "profile", "") or "",
        "backend": getattr(args, "backend", "") or "",
        "input_mf4": getattr(args, "input_mf4", "") or "",
        "input_path": getattr(args, "input_path", "") or "",
        "dataset": getattr(args, "dataset", "") or "",
        "output_mf4": getattr(args, "output_mf4", "") or "",
        "mode": getattr(args, "mode", "") or "",
        "run_id": getattr(args, "run_id", "") or "",
        "timeout": int(getattr(args, "timeout", 0) or 0),
        "limit": int(getattr(args, "limit", 0) or 0),
        "max_duration": int(getattr(args, "max_duration", 0) or 0),
        "stall_timeout": int(getattr(args, "stall_timeout", 0) or 0),
        "max_minutes": int(getattr(args, "max_minutes", 0) or 0),
        "clean": bool(getattr(args, "clean", False)),
        "deps": bool(getattr(args, "deps", False)),
        "dry_run": bool(getattr(args, "dry_run", False)),
        "execute": bool(getattr(args, "execute", False)),
        "copy_data": bool(getattr(args, "copy_data", False)),
        "copy_selena": bool(getattr(args, "copy_selena", False)),
        "no_progress": bool(getattr(args, "no_progress", False)),
        "select": bool(getattr(args, "select", False)),
        "no_retry": bool(getattr(args, "no_retry", False)),
        "no_wait": bool(getattr(args, "no_wait", False)),
        "no_fetch": bool(getattr(args, "no_fetch", False)),
        "required_signals": list(getattr(args, "required_signal", []) or []),
        "extra_args": list(getattr(args, "extra_arg", []) or []),
    }
    for key, value in cli_overrides.items():
        # Only let a CLI flag override the JSON payload when it was actually
        # provided (non-empty / non-default). This keeps --payload-json the
        # authoritative source for fields the CLI doesn't expose (e.g. project
        # when invoked through the global rsim --project).
        if value not in ("", [], 0, False):
            task_payload[key] = value
    task_payload = {key: value for key, value in task_payload.items() if value not in ("", [], 0, False)}
    return _print_json(_service_from_args(args).create_job(args.job_type, payload=task_payload, metadata=metadata))


def _service_from_args(args) -> ControlService:
    return ControlService(_db_path_from_args(args))


def _db_path_from_args(args) -> Path:
    db_path = getattr(args, "db_path", "") or ""
    if db_path:
        return Path(db_path)
    # Default: per-user DB (isolates jobs/logs between users on a shared server).
    from core.user import control_db_path_for_user
    return control_db_path_for_user()


def _parse_json_flag(raw: str, label: str) -> dict:
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{label} JSON must be an object")
    return value


def _ensure_utf8_stdout() -> None:
    """Force stdout to UTF-8 so JSON with non-ASCII (e.g. Chinese check output
    in task logs) doesn't crash on cp936/charmap Windows terminals.

    Mirrors the agent's UTF-8 fix (cli/agent.py) for the server CLI's print
    path. Safe no-op on POSIX where stdout is already UTF-8. Python 3.7+.
    """
    import sys
    stream = getattr(sys.stdout, "reconfigure", None)
    if stream is not None:
        try:
            stream(encoding="utf-8", errors="replace")
        except (TypeError, ValueError, OSError):
            pass


def _print_json(payload) -> int:
    _ensure_utf8_stdout()
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0
