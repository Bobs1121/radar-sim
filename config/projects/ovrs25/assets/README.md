# ovrs25 project assets

Maintain project-owned Selena assets in this folder:

- `selena/selena_config_tmpl.txt`: real Selena paramconfig template kept in-repo.
- `runtime.xml`: project runtime XML used by Selena.
- `matfilefilter.txt`: project MATLAB filter file.

`prepare-sim` / `render_selena_config` renders the template into a generated runtime folder under `results/<project>/_runtime/` by default. If a machine still requires a fixed `C:/tools/...` location, override it in `config/projects/<project>/local.yaml`.
