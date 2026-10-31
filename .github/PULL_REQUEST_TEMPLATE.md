## What changed and why

<!-- One or two paragraphs: the problem, the approach, and the related issue (Closes #123). -->

## Checks run

The checks are those of [CONTRIBUTING.md, "Checks before a pull request"](https://github.com/seuclkzsd/EdgeFDL/blob/main/CONTRIBUTING.md#checks-before-a-pull-request) and CI.

<!-- Tick the checks you actually ran and paste the end of their output (for example "Ran 220 tests ... OK"). -->

- [ ] `python -m unittest discover -s tests`
- [ ] `python tools/dev/spdx.py`
- [ ] `tools/dev/ci.sh quick`
- [ ] `tools/dev/ci.sh image` (when changing `Dockerfile`, `compose.yaml`, `.dockerignore`, `requirements-lock.txt` or the `serve` command)
- [ ] `scripts/lab.sh smoke` (when changing `edgefdl/vnet/`, `edgefdl/faults/injector.py` or `edgefdl/measure/probes.py`)
- [ ] LXD dry run reviewed (when changing `edgefdl/vnet/lxd.py` or `edgefdl/vnet/stack.py`)

```text
<paste the summary of the checks here>
```

## Tests added or changed

<!-- List test files and test names. For a bug fix, name the test that fails before the change and passes after it. -->

## Impact on accuracy and robustness numbers

- [ ] No change to alarms or to any published number (refactoring, documentation, tests, tooling)
- [ ] Changes behaviour but not the detection, diagnosis or alarm logic (describe the scope)
- [ ] **Changes detection, diagnosis or alarm logic, fault semantics, the agent or sample generation**: an issue was opened first, a test that failed before the change is included, and the output of `scripts/compare_robust.py` on seeds 21, 33 and 47 (`ALL PASS`) is pasted below, as required by [GOVERNANCE.md](https://github.com/seuclkzsd/EdgeFDL/blob/main/GOVERNANCE.md)
- [ ] Published numbers change: the pipeline and/or robustness suite were re-run, the raw outputs are committed to `results/`, and README.md, README.zh-CN.md and docs/results.md were updated from them

<!-- For any number you quote: backend, topology, sample count, seed, CPU, Python and PyTorch versions. -->

## Interfaces and documentation

- [ ] No change to CLI options, REST endpoints or their JSON fields, Prometheus metrics, CSV log columns, topology or intake formats, or the model bundle format
- [ ] One of these interfaces changed, and docs/api.md and the other affected documents are updated
- [ ] User-visible changes are recorded in the `[Unreleased]` section of `CHANGELOG.md`
- [ ] Topologies, configurations and logs in this pull request are synthetic or anonymized; new dependencies are recorded in `requirements.txt`, `pyproject.toml`, `requirements-lock.txt`, `NOTICE` and `docs/dependencies.md`
- [ ] If AI tools were used substantially, this is mentioned above, and I have reviewed and tested every line myself
