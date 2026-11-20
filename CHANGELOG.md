# Changelog

The version is `car_use.__version__` in `src/car_use/__init__.py`. Pushing a tag `vX.Y.Z` that matches it publishes that version to PyPI.

## Unreleased

- `car-use can-check`: listens to the CAN bus on the OBD port without sending anything, and says whether the car's own traffic reaches it (open, partial, hidden). `--find steering,brake,indicator` guides the person through each and lists the message bytes that moved. `--report` prints anonymised JSON for a Port report.
- `car-use record`: logs OBD readings and CAN frames to CSV with UTC timestamps, for lining up with a dashcam.
- `listen()` helper, the `open-bus` virtual car, and monitor mode (ATMA) with id filters and BUFFER FULL in the simulator.

## 0.0.1

- OBD-II harness, virtual cars, adapter grading, and the safety kernel.
- Install with `uv tool install car-use` after this version is on PyPI.
