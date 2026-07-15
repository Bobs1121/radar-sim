"""rsim agent-binding — local workspace binding management (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.agent_bindings import AgentBindingStore
from core.agent_data_bindings import AgentDataBindingStore
from core.agent_asset_bindings import AgentAssetBindingStore

NO_CONFIG = True


def register(subparsers):
    parser = subparsers.add_parser(
        "agent-binding",
        help="Manage local workspace bindings for agent artifact staging",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # register
    p_register = sub.add_parser("register", help="Register a workspace binding")
    p_register.add_argument("--project", required=True, help="Project logical token")
    p_register.add_argument(
        "--workspace-root", required=True, help="Path to the workspace root directory"
    )
    p_register.add_argument(
        "--output-root",
        action="append",
        required=True,
        help="Repeatable output root directory (must be inside workspace-root)",
    )
    p_register.add_argument(
        "--db", default=None, help="Path to the SQLite binding store (default: ~/.rsim/agent/bindings.db)"
    )

    # list
    p_list = sub.add_parser("list", help="List registered bindings")
    p_list.add_argument("--project", default=None, help="Filter by project token")
    p_list.add_argument("--db", default=None, help="Path to the SQLite binding store")
    p_list.add_argument("--json", action="store_true", help="Emit JSON array")

    # health
    p_health = sub.add_parser("health", help="Check binding health")
    p_health.add_argument("--binding-id", required=True, help="Binding identifier")
    p_health.add_argument("--project", required=True, help="Project logical token")
    p_health.add_argument("--db", default=None, help="Path to the SQLite binding store")

    # delete
    p_delete = sub.add_parser("delete", help="Delete a binding")
    p_delete.add_argument("--binding-id", required=True, help="Binding identifier")
    p_delete.add_argument("--db", default=None, help="Path to the SQLite binding store")

    p_data_register = sub.add_parser("data-register", help="Authorize a local MF4 root for Agent upload")
    p_data_register.add_argument("--project", required=True, help="Project logical token")
    p_data_register.add_argument("--data-root", required=True, help="Readable local MF4 directory")
    p_data_register.add_argument("--db", default=None, help="Path to the SQLite binding store")

    p_data_list = sub.add_parser("data-list", help="List path-free local data-root bindings")
    p_data_list.add_argument("--project", default=None, help="Filter by project token")
    p_data_list.add_argument("--db", default=None, help="Path to the SQLite binding store")
    p_data_list.add_argument("--json", action="store_true", help="Emit JSON array")

    p_data_delete = sub.add_parser("data-delete", help="Delete a local data-root binding")
    p_data_delete.add_argument("--binding-id", required=True, help="Data binding identifier")
    p_data_delete.add_argument("--db", default=None, help="Path to the SQLite binding store")

    p_asset_register = sub.add_parser("asset-register", help="Authorize a folder containing Runtime/Adapter/MatFilter files")
    p_asset_register.add_argument("--asset-root", required=True, help="Readable configuration asset directory")
    p_asset_register.add_argument("--db", default=None, help="Path to the SQLite binding store")

    p_asset_list = sub.add_parser("asset-list", help="List path-free configuration asset bindings")
    p_asset_list.add_argument("--db", default=None, help="Path to the SQLite binding store")
    p_asset_list.add_argument("--json", action="store_true", help="Emit JSON array")

    p_asset_delete = sub.add_parser("asset-delete", help="Delete a configuration asset binding")
    p_asset_delete.add_argument("--binding-id", required=True, help="Asset binding identifier")
    p_asset_delete.add_argument("--db", default=None, help="Path to the SQLite binding store")


def run(args, _config):
    subcommand = args.subcommand

    if subcommand.startswith("asset-"):
        store = AgentAssetBindingStore(db_path=getattr(args, "db", None))
        if subcommand == "asset-register":
            binding = store.register(args.asset_root)
            print(json.dumps(binding.public_dict, sort_keys=True, ensure_ascii=False))
            return 0
        if subcommand == "asset-list":
            publics = [item.public_dict for item in store.list()]
            if args.json:
                print(json.dumps(publics, sort_keys=True, ensure_ascii=False))
            else:
                for public in publics:
                    print(json.dumps(public, sort_keys=True, ensure_ascii=False))
            return 0
        if subcommand == "asset-delete":
            store.delete(args.binding_id)
            print(json.dumps({"id": args.binding_id, "deleted": True}, sort_keys=True))
            return 0

    if subcommand.startswith("data-"):
        store = AgentDataBindingStore(db_path=getattr(args, "db", None))
        if subcommand == "data-register":
            binding = store.register(project=args.project, root_path=args.data_root)
            print(json.dumps(binding.public_dict, sort_keys=True, ensure_ascii=False))
            return 0
        if subcommand == "data-list":
            publics = [item.public_dict for item in store.list(project=getattr(args, "project", None) or "")]
            if args.json:
                print(json.dumps(publics, sort_keys=True, ensure_ascii=False))
            else:
                for public in publics:
                    print(json.dumps(public, sort_keys=True, ensure_ascii=False))
            return 0
        if subcommand == "data-delete":
            store.delete(args.binding_id)
            print(json.dumps({"id": args.binding_id, "deleted": True}, sort_keys=True))
            return 0

    store = _open_store(args)

    if subcommand == "register":
        return _cmd_register(args, store)
    if subcommand == "list":
        return _cmd_list(args, store)
    if subcommand == "health":
        return _cmd_health(args, store)
    if subcommand == "delete":
        return _cmd_delete(args, store)

    return 1


def _open_store(args) -> AgentBindingStore:
    db = getattr(args, "db", None)
    return AgentBindingStore(db_path=db)


def _cmd_register(args, store: AgentBindingStore) -> int:
    workspace_root = Path(args.workspace_root)
    output_roots = tuple(args.output_root)

    binding = store.register(
        project=args.project,
        workspace_root=workspace_root,
        output_roots=output_roots,
    )

    print(json.dumps(binding.public_dict, sort_keys=True, ensure_ascii=False))
    return 0


def _cmd_list(args, store: AgentBindingStore) -> int:
    project = getattr(args, "project", None) or None
    bindings = store.list(project=project)

    publics = [b.public_dict for b in bindings]

    if args.json:
        print(json.dumps(publics, sort_keys=True, ensure_ascii=False))
    else:
        for pub in publics:
            print(json.dumps(pub, sort_keys=True, ensure_ascii=False))
    return 0


def _cmd_health(args, store: AgentBindingStore) -> int:
    roots = store.resolve_authorized_roots(binding_id=args.binding_id, project=args.project)

    result: dict[str, Any] = {
        "id": args.binding_id,
        "project": args.project,
        "healthy": True,
        "output_root_count": len(roots.output_roots),
    }
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


def _cmd_delete(args, store: AgentBindingStore) -> int:
    store.delete(binding_id=args.binding_id)

    result = {
        "id": args.binding_id,
        "deleted": True,
    }
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0
