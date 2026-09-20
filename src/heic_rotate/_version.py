"""Single source of truth for heic_rotate's version.

Editing this file's VERSION string is the ONLY step needed to bump the
package version - pyproject.toml reads it dynamically (see
`[tool.setuptools.dynamic]`), and heic_rotate.core reads it for the
`-V`/`--version` output and for the provenance record's TVER field.

Kept as its own tiny module (rather than living in __init__.py or
core.py) specifically so setuptools' `attr:` version-reading can import
it in isolation at build time, without needing the rest of the package
(or its dependencies, of which there currently are none) importable
first.
"""

VERSION = '1.7.0'
