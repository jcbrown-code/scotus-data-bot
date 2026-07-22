"""Transform stage: raw mirror -> normalized, de-duplicated corpus.

Each sub-stage is a module, run in order: materialize -> scope -> dedup ->
validate -> reselect -> clean.
"""
