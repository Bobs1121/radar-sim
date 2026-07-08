from __future__ import annotations

from pathlib import Path


class RecipeHandler:
    recipe_name = "default"

    def validate(self, config: dict) -> list[str]:
        return []

    def prepare_simulation(self, config: dict, sim: dict, *, stage: str) -> dict:
        return sim

    def prepare_repo_context(self, config: dict, default_prepare) -> str:
        return default_prepare(config)

    def default_script_args_template(self, config: dict) -> list[str]:
        return []

    def shape_selena_script_args(self, config: dict, mode: str) -> list[str]:
        build = config.get("build", {})
        build_config_full = build.get("build_config") or config.get("paths", {}).get("build_config", "")
        build_config_name = Path(str(build_config_full)).stem if build_config_full else ""
        binding = config.get("binding", "")
        template = build.get("script_args_template")
        if template is None:
            template = self.default_script_args_template(config)

        format_ctx = {
            "binding": binding,
            "build_config": str(build_config_full),
            "build_config_name": build_config_name,
            "build_mode": mode,
            "inner_repo_root": config.get("repos", {}).get("inner_repo_root", ""),
            "outer_repo_root": config.get("repos", {}).get("outer_repo_root", ""),
            "project_root": config.get("project_root", ""),
        }
        return [
            str(item).format(**format_ctx)
            for item in template
            if str(item).format(**format_ctx) != ""
        ]


class DefaultRecipe(RecipeHandler):
    recipe_name = "default"

    def default_script_args_template(self, config: dict) -> list[str]:
        binding = config.get("binding", "")
        return ["{build_mode}", "{build_config_name}", "{binding}"] if binding else []


class G3nFvg3Od25Recipe(RecipeHandler):
    recipe_name = "g3n_fvg3_od25"

    def default_script_args_template(self, config: dict) -> list[str]:
        return []

    def validate(self, config: dict) -> list[str]:
        adapter_file = config.get("simulation", {}).get("adapter_file", "")
        if adapter_file:
            return []
        return [f"Recipe {self.recipe_name} requires simulation.adapter_file"]


_registry: dict[str, type[RecipeHandler]] = {}


def register(cls: type[RecipeHandler]) -> type[RecipeHandler]:
    _registry[cls.recipe_name] = cls
    return cls


def get(name: str) -> RecipeHandler:
    cls = _registry.get(name) or _registry["default"]
    return cls()


def list_all() -> list[str]:
    return sorted(_registry.keys())


def get_for_config(config: dict) -> RecipeHandler:
    meta = config.get("_meta", {})
    project = config.get("project", {})
    recipe_name = meta.get("recipe") or project.get("recipe") or "default"
    return get(recipe_name)


register(DefaultRecipe)
register(G3nFvg3Od25Recipe)