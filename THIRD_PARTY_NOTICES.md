# Third-party components

The MIT license covers this repository's project code, not third-party software.
Dependencies are referenced rather than vendored. Review their own license files:

| Component | Purpose | Upstream license |
| --- | --- | --- |
| Python | Interpreter and standard library, including Tk bindings | PSF License |
| openpyxl | Optional XLSX adjustment and test support | MIT |
| et_xmlfile | openpyxl dependency | MIT |
| xlrd | Optional legacy XLS reading | BSD |
| xlwt | Optional legacy XLS writing | BSD |
| xlutils | Optional legacy XLS copying | MIT |

Windchill is a third-party product. The optional exporter executable, interface DLL,
runtime configuration, and database command scripts are not distributed here. Their
availability, permissions, licenses and compatibility must be established separately.
The adapter code does not grant rights to those components or to Windchill itself.

No proprietary component is relicensed under MIT by this repository.
