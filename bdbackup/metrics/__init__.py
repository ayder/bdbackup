"""The ``bdbackup metrics`` command: Prometheus textfile metrics read from the history database.

The modules here import neither :mod:`bdbackup.config` nor :mod:`bdbackup.cli`, because the
configuration imports the engine registry, and that refers to the collector protocol.
"""
