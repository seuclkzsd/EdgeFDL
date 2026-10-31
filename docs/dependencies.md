<!-- SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Dependencies and licenses

EdgeFDL is licensed under the [Apache License 2.0](../LICENSE). This page lists everything it depends on, the license
of each item and where the boundary of what this repository distributes lies. [NOTICE](../NOTICE) carries the same
information in short form. Three checks keep the page true:

| Check | Command | What fails it |
|---|---|---|
| Licenses of the installed Python runtime | `python tools/dev/licenses.py` (`tools/dev/ci.sh deps`) | A package with an unknown or strong-copyleft license in the dependency closure of [`requirements.txt`](../requirements.txt) |
| The lock as installed in the image | `tools/dev/ci.sh image` | A package that differs from [`requirements-lock.txt`](../requirements-lock.txt), a dependency outside the lock, or a table below that differs from what `licenses.py --lock requirements-lock.txt --markdown` prints inside the image |
| License headers of this repository | `python tools/dev/spdx.py` (`tools/dev/ci.sh licenses`) | A source file without an SPDX header |

The license of a package is read from its installed metadata: `License-Expression`, then the `License` field, then the
license classifiers, then the text of its license files. `licenses.py` treats MIT, BSD, Apache-2.0, PSF-2.0, ISC,
Unlicense, Zlib and similar as permissive; LGPL, MPL, EPL and a GPL with a linking exception as weak copyleft, which
passes; GPL, AGPL, SSPL, OSL and EUPL as strong copyleft, which fails; anything it cannot read as unknown, which
fails. The CUDA build of PyTorch also pulls in NVIDIA's proprietary `nvidia-*` libraries: they are not part of the
CPU install, the only install this project needs, and `licenses.py` lists their number without judging them.

## Python runtime

These are all 13 packages of the CPU install on Python 3.12, linux/amd64, with the exact versions of
[`requirements-lock.txt`](../requirements-lock.txt). EdgeFDL itself imports only NumPy, PyYAML, PyTorch and,
optionally, psutil; the other nine are dependencies of PyTorch.

<!-- licenses:begin (printed by tools/dev/licenses.py --lock requirements-lock.txt --markdown inside the image) -->
| Package | Version | License | Required by |
|---|---|---|---|
| filelock | 4.2.5 | MIT | torch |
| fsspec | 2026.9.0 | BSD-3-Clause | torch |
| Jinja2 | 3.1.6 | BSD-3-Clause | torch |
| MarkupSafe | 3.0.4 | BSD-3-Clause | jinja2 |
| mpmath | 1.3.0 | BSD-3-Clause | sympy |
| networkx | 3.7 | BSD-3-Clause | torch |
| numpy | 2.3.3 | BSD-3-Clause | direct |
| psutil | 7.2.2 | BSD-3-Clause | direct |
| PyYAML | 6.0.3 | MIT | direct |
| setuptools | 84.2.1 | MIT | torch |
| sympy | 1.14.0 | BSD-3-Clause | torch |
| torch | 2.9.1+cpu | BSD-3-Clause | direct |
| typing_extensions | 4.16.0 | PSF-2.0 | torch |
<!-- licenses:end -->

`requirements.txt` and `pyproject.toml` state the minimum versions; other versions may be installed outside the image.
The PyTorch version in the lock is the one of the published results, 2.9.1, here as its CPU build. The published results
used Python 3.13 and the image uses Python 3.12; evaluating `results/model.pt` on the test set inside the image
reproduces `results/eval_test.json` byte for byte ([`results/docker_check.json`](../results/docker_check.json)).

### Native libraries inside the wheels

Two wheels carry native libraries, with the license files of the wheel as the source of this table. They come with the
wheels from their upstream projects; this repository does not contain or modify them.

| Wheel | Library | License |
|---|---|---|
| numpy | `numpy.libs/libscipy_openblas64_*.so` (OpenBLAS, with LAPACK) | BSD-3-Clause, BSD-3-Clause-Open-MPI |
| numpy | `numpy.libs/libgfortran-*.so` (GCC runtime library) | GPL-3.0-or-later WITH GCC-exception-3.1 |
| numpy | `numpy.libs/libquadmath-*.so` (GCC runtime library) | LGPL-2.1-or-later |
| torch | `torch/lib/libgomp.so.1` (GCC OpenMP runtime) | GCC runtime library; the wheel does not list it separately, upstream GCC licenses it under GPL-3.0-or-later WITH GCC-exception-3.1 |

The wheel of PyTorch lists its bundled third-party components in `torch-2.9.1+cpu.dist-info/licenses/LICENSE` and
`NOTICE`; the components named there are under MIT, BSD, Apache-2.0, BSL-1.0, public domain or similar terms. The same
file reproduces license texts found anywhere in the source tree of those components, which includes a GPL-3.0 text for
the test directory of `cpr` and an LGPL-2.1 text for the `ffnvcodec` port of `vcpkg`.

## External programs

The `netns` and `lxd` backends of the equivalent virtual network and the real-stack lab container run the programs
below. **They run as separate programs.** EdgeFDL starts them as child processes through their command-line
interfaces, as argument lists without a shell ([SECURITY.md](../SECURITY.md#scope)). **They are not linked**: EdgeFDL
contains no code of theirs and loads no library of theirs. **They are not redistributed by this repository**: the
source tree, the wheel, the sdist and the release assets do not contain them, and no container image is published. They
are installed from the operating system's packages on the machine that uses those backends.

The `sim` backend, training, evaluation and the dashboard in simulation mode run none of them, and the image of the
root [`Dockerfile`](../Dockerfile) does not contain them.

| Program | Used for | Debian 12 package in the lab image | License (Debian `copyright` file) |
|---|---|---|---|
| FRRouting (`zebra`, `ospfd`, `vtysh`) | OSPF routing, LSDB reads | frr 8.4.4-1.1~deb12u2 | GPL-2.0-or-later; some libraries LGPL-2.1-or-later, ISC, MIT or BSD |
| iproute2 (`ip`, `tc`) | Namespaces, links, fault injection | iproute2 6.1.0-3 | GPL-2.0 |
| iperf3 | Bandwidth of a deep probe | iperf3 3.12-1+deb12u2 | BSD-3-Clause; bundled parts under other permissive licenses |
| traceroute | Path measurement | traceroute 1:2.1.2-1 | GPL-2.0-or-later; `libsupp` LGPL-2.1-or-later |
| iputils (`ping`) | Light and deep probes | iputils-ping 3:20221126-1+deb12u1 | BSD-3-Clause for `ping`; some files of the source package GPL-2.0-or-later |
| procps (`sysctl`) | Per-namespace kernel settings | procps 2:4.0.2-3 | GPL-2.0-or-later for `sysctl`; most of the package LGPL-2.1-or-later |
| LXD (`lxc` client) | `lxd` backend only; not in the lab image | installed by the operator | AGPL-3.0-only since LXD 5.20, Apache-2.0 up to 5.19; the client SDKs remain Apache-2.0 |

Package versions are in [`results/lab_packages.txt`](../results/lab_packages.txt), printed by `dpkg-query` in the lab
image.

## Container images

Neither image below is published by the project: each is built on the user's machine from the files in this repository.

| Image | Built from | Contains |
|---|---|---|
| Root [`Dockerfile`](../Dockerfile) (`docker compose up`) | `python:3.12-slim-bookworm`, pinned by digest, and the 13 packages above | CPython 3.12 (PSF-2.0), Debian 12 base packages under their own licenses (listed in `/usr/share/doc/*/copyright` in the image), the packages above, `edgefdl/`, `configs/demo_topology.yaml`, `results/model.pt`, `LICENSE`, `NOTICE` |
| [`scripts/lab/Dockerfile`](../scripts/lab/Dockerfile) (`scripts/lab.sh build`) | `debian:bookworm-slim` and Debian packages | The external programs above and Python; privileged, for development only |

Whoever publishes an image built from these files distributes its contents and takes on their obligations, for
example the source offer of the GPL and LGPL components in the Debian base image.

## Development tools

Not needed at run time and not part of any package or image.

| Tool | Used by | License |
|---|---|---|
| [ruff](https://github.com/astral-sh/ruff) | `tools/dev/ci.sh lint` | MIT |
| [coverage.py](https://github.com/nedbat/coveragepy) | `tools/dev/ci.sh coverage` | Apache-2.0 |
| [build](https://github.com/pypa/build) | `tools/dev/ci.sh dist` | MIT |
| [matplotlib](https://matplotlib.org) | `tools/figures/` (the figures in `docs/`), installed by hand | Matplotlib License (PSF-style) |
| Docker with the compose plugin | Images, `tools/dev/ci.sh image`, `tools/dev/ci.sh realstack` | Apache-2.0 |

The versions of ruff, coverage.py and build are pinned in [`requirements-dev.txt`](../requirements-dev.txt).

## Updating

Dependabot proposes updates of the pinned development tools, the GitHub Actions and the locked packages, except
PyTorch: its CPU wheels are not on PyPI, and its version belongs to the conditions of the published results. To
regenerate the lock (Docker needed; set `EDGEFDL_REGISTRY` or `PIP_INDEX_URL` to use mirrors):

```bash
python tools/dev/lock.py          # resolves ROOTS of that file in the base image, writes requirements-lock.txt
tools/dev/ci.sh image             # fails until the table above matches
```

A new runtime dependency needs an OSI-approved license and an entry in `requirements.txt`, `pyproject.toml`,
`requirements-lock.txt`, [NOTICE](../NOTICE) and this page. The base image digest is in the first `FROM` line of the
root `Dockerfile`.
