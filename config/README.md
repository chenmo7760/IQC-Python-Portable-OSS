# Local configuration

`procedure_aliases.json` is empty. `procedure_aliases.example.json` shows one disabled,
synthetic exact alias. No mapping is inferred from the example. Duplicate/conflicting
keys fail closed. Maintain deployment mappings privately.

Copy `engineers.example.json` to `engineers.json` for desktop exploration. Its one
synthetic name is not a real account. Resolve real account IDs only in an authorized
private environment. Local engineer and launcher configuration are ignored by Git.

Optional `launcher.json` settings include `entry_api_base_url`, `output_dir`,
`paper_standard_catalog_path`, and the template suffix map read by `src.config`.
Omitted endpoints and paths remain unconfigured. `IQC_OUTPUT_DIR` can explicitly set
an output directory. `IQC_ENTRY_API_BASE_URL` overrides the optional API endpoint.
Never commit authentication headers, connection files, real personnel, or site data.

Inspection-adjustment source and target paths start empty; select them explicitly in
the desktop file picker. Remembered preferences are local files, not published defaults.
The optional database BAT scripts and Windchill exporter must be supplied separately
by an authorized integrator. They are not required by the offline demonstration.
