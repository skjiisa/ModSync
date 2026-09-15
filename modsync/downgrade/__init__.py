"""Game runtime downgrades: recipe index, patch fetching, xdelta3 application.

The heavy lifting (which files to patch, with what) comes from a community
recipe that ModSync converts into a small JSON index it controls. See
``mulderload.py`` for the converter and ``recipes/`` for the generated index.
"""
