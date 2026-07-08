from core.recipes import DefaultRecipe, G3nFvg3Od25Recipe, get_for_config


def test_get_for_config_falls_back_to_default_recipe():
    handler = get_for_config({})

    assert isinstance(handler, DefaultRecipe)
    assert handler.recipe_name == "default"


def test_get_for_config_resolves_g3n_recipe_handler():
    handler = get_for_config({"_meta": {"recipe": "g3n_fvg3_od25"}})

    assert isinstance(handler, G3nFvg3Od25Recipe)
    assert handler.recipe_name == "g3n_fvg3_od25"


def test_default_recipe_uses_binding_style_args_without_template():
    handler = get_for_config({})
    config = {
        "binding": "ovrs25",
        "build": {
            "build_config": "full_dsp",
        },
    }

    args = handler.shape_selena_script_args(config, "Release")

    assert args == ["Release", "full_dsp", "ovrs25"]


def test_g3n_recipe_uses_empty_args_without_template():
    handler = get_for_config({"_meta": {"recipe": "g3n_fvg3_od25"}})
    config = {
        "binding": "ovrs25",
        "build": {
            "build_config": "full_dsp",
        },
    }

    args = handler.shape_selena_script_args(config, "Release")

    assert args == []


def test_g3n_recipe_requires_adapter_file():
    handler = get_for_config({"_meta": {"recipe": "g3n_fvg3_od25"}})

    issues = handler.validate({"simulation": {}})

    assert issues == ["Recipe g3n_fvg3_od25 requires simulation.adapter_file"]