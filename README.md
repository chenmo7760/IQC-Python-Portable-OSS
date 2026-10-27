# IQC Python Portable OSS

A Python toolkit for reading inspection worksheets, assembling draft workbooks,
checking exact template matches, and separating preview from submission. It includes
a Tk desktop interface and optional adapters for external systems.

## Offline quick start

Python 3.12 is the verified source runtime. The demonstration uses only the Python
standard library and the two included synthetic workbooks. It makes no network requests,
starts no external programs, and does not require Excel or a database.

```powershell
python -m src.demo --output outputs/demo
```

The command writes one draft workbook from two synthetic input rows and checks its
saved values. All names, organizations, identifiers and test data in this repository
are synthetic. The alias configuration is empty by default. The sample is not an
approved inspection procedure, business acceptance result, or production form.

## Tests and desktop interface

Install dependencies while connected, or prepare a local wheel directory on another
machine, then install with `python -m pip install --no-index --find-links wheels -r requirements.txt`.

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests
Copy-Item config/engineers.example.json config/engineers.json
python -m src.main
```

Tk support must be available in the Python installation. Desktop operations that need
external data are blocked until explicitly configured. See [configuration](config/README.md).
The command-line demo remains usable without these optional integrations.

## Scope and integration boundaries

This source release includes synthetic input/output templates, parsing, aggregation,
validation, preview, local spreadsheet generation, and adapter contracts. No live
database, account list, deployed executable, proprietary DLL, production mapping,
connection configuration or original repository history is included.

The existing adapter schemas may require adaptation for another system. Database and
HTTP integration, end-to-end submission, packaged executables, Windows 7 compatibility,
and operation against real inspection records were not accepted in this release.
Do not use demo labels or synthetic rule groups as production configuration.

## License

Project code is licensed under [MIT](LICENSE), copyright 2026 chenmo7760.
Dependencies and optional proprietary integrations retain their own terms; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Known test limitations

Two existing PQC pane-width tests are explicit expected failures: one requests a
238 px scope pane while the implementation caps it at 200 px; the other expects a
320 px minimum while the implementation permits 180 px. Both were reproduced on
the unmodified source before this public release. They are retained as visible
regression signals. No UI layout behavior was changed to hide these mismatches.
One export-helper test is skipped because the optional external backend source is
not distributed here. Unit tests and the synthetic demo
do not establish real-system acceptance.
