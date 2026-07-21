"""Transform stage: raw mirror -> normalized, de-duplicated corpus.

Each sub-stage is a module (materialize, scope, ...). The deprecated V1
monolith lives at ``src/transform_legacy.py`` until its importers migrate.
"""
