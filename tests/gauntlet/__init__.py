"""
The autonomous-node reliability gauntlet.

Fault-injection tests that simulate the failures a beta node meets in the
field — cloud outages, corrupt files, dead devices, daytime plan delivery,
host sleep, full disks — and assert that the node either recovers safely or
produces structured evidence a remote operator can diagnose from.

Run the whole gauntlet:

    python -m unittest discover -s tests/gauntlet -t .

Each test module maps to failure modes in
docs/reliability/failure_mode_map.md (F-numbers referenced in docstrings).
"""
