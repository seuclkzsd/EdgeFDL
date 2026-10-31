# SPDX-FileCopyrightText: 2026 seuclkzsd and the EdgeFDL contributors
# SPDX-License-Identifier: Apache-2.0
"""打包与治理文件的一致性：许可证检查、锁文件、镜像配置、标签集。不需要 Docker 和网络。"""
import importlib.util
import re
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def tool(name):
    spec = importlib.util.spec_from_file_location(f"edgefdl_dev_{name}", ROOT / "tools" / "dev" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


licenses = tool("licenses")
lock = tool("lock")
labels = tool("labels")


def read(*parts):
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


class LicenseCheckTest(unittest.TestCase):
    def test_expressions(self):
        L = licenses
        for expr, want in (("MIT", L.PERMISSIVE), ("BSD-3-Clause AND 0BSD AND Zlib", L.PERMISSIVE),
                           ("Apache-2.0 OR GPL-2.0-only", L.PERMISSIVE), ("MPL-2.0", L.WEAK),
                           ("MIT AND LGPL-2.1-or-later", L.WEAK), ("GPL-3.0-or-later", L.STRONG),
                           ("MIT AND AGPL-3.0-only", L.STRONG), ("(MIT OR GPL-2.0-only) AND GPL-3.0-only", L.STRONG),
                           ("GPL-3.0-or-later WITH GCC-exception-3.1", L.WEAK), ("LicenseRef-Proprietary", L.UNKNOWN),
                           ("Proprietary software of NVIDIA", L.UNKNOWN), ("MIT AND (", L.UNKNOWN), ("", L.UNKNOWN)):
            self.assertEqual(L.rank_expression(expr), want, expr)

    def test_license_text(self):
        bsd2 = "Redistribution and use in source and binary forms, with or without modification, are permitted"
        self.assertEqual(licenses.fingerprint(bsd2 + " provided. Neither the name of X"), "BSD-3-Clause")
        self.assertEqual(licenses.fingerprint(bsd2), "BSD-2-Clause")
        self.assertEqual(licenses.fingerprint(bsd2 + " ... Permission is hereby granted, free of charge"), "BSD-2-Clause")
        self.assertEqual(licenses.fingerprint("GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007"), "GPL-3.0-only")
        self.assertEqual(licenses.fingerprint("All rights reserved."), "")

    def test_installed_runtime_passes(self):
        dists, _, missing, _ = licenses.walk(["numpy", "pyyaml"])
        self.assertFalse(missing)
        for d in dists.values():
            self.assertLess(licenses.rank_expression(licenses.declared(d)), licenses.STRONG, d.metadata["Name"])

    def test_gpl_metadata_fails(self):
        class Dist:
            metadata = {"Name": "x"}
            files = None

        class Meta(dict):
            def get_all(self, key):
                return ["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"] if key == "Classifier" else None

        Dist.metadata = Meta(Name="x", License="MIT")
        self.assertGreaterEqual(licenses.rank_expression(licenses.declared(Dist)), licenses.STRONG)


class LockTest(unittest.TestCase):
    def test_render_is_sorted_and_hashed(self):
        report = {"environment": {"python_version": "3.12", "platform_machine": "x86_64"}, "install": [
            {"metadata": {"name": "Z_pkg", "version": "1"}, "download_info": {"archive_info": {"hashes": {"sha256": "ab"}}}},
            {"metadata": {"name": "a", "version": "2+cpu"}, "download_info": {"archive_info": {"hashes": {"sha256": "cd"}}}}]}
        text = lock.render(report)
        self.assertLess(text.index("a==2+cpu"), text.index("z-pkg==1"))
        self.assertIn("--hash=sha256:ab", text)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.txt"
            p.write_text(text, encoding="utf-8")
            self.assertEqual(licenses.read_lock(p), {"a": "2+cpu", "z-pkg": "1"})
        report["environment"]["python_version"] = "3.11"
        with self.assertRaises(SystemExit):
            lock.render(report)

    def test_lock_covers_roots_and_is_fully_hashed(self):
        pins = licenses.read_lock(ROOT / "requirements-lock.txt")
        self.assertEqual(read("requirements-lock.txt").count("--hash=sha256:"), len(pins))
        for root in lock.ROOTS:
            name, ver = root.split("==")
            self.assertEqual(pins[licenses.norm(name)], ver)
        for name in licenses.read_requirements(ROOT / "requirements.txt"):
            self.assertIn(name, pins)
        self.assertTrue(pins["torch"].endswith("+cpu"))
        self.assertFalse([n for n in pins if n.startswith("nvidia")])

    def test_documented_table_matches_lock(self):
        doc = read("docs", "dependencies.md")
        table = doc[doc.index("<!-- licenses:begin"):doc.index("<!-- licenses:end")]
        rows = re.findall(r"^\| (\S+) \| (\S+) \| (\S+) \| ", table, re.M)[1:]    # 跳过表头
        got = {licenses.norm(n): v for n, v, _ in rows}
        self.assertEqual(got, licenses.read_lock(ROOT / "requirements-lock.txt"))
        for _, _, lic in rows:
            self.assertEqual(licenses.rank_expression(lic), licenses.PERMISSIVE, lic)


class ImageConfigTest(unittest.TestCase):
    def test_compose_least_privilege(self):
        svc = yaml.safe_load(read("compose.yaml"))["services"]["edgefdl"]
        self.assertTrue(svc["read_only"])
        self.assertEqual(svc["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", svc["security_opt"])
        self.assertNotIn("privileged", svc)
        self.assertNotIn("network_mode", svc)
        self.assertTrue(all(p.startswith("127.0.0.1:") for p in svc["ports"]), "只发布到回环地址")

    def test_dockerfile(self):
        text = read("Dockerfile")
        self.assertRegex(text, r"(?m)^FROM \$\{REGISTRY\}/python:3\.12-slim-bookworm@sha256:[0-9a-f]{64}$")
        self.assertIn("--no-deps --require-hashes -r /tmp/requirements-lock.txt", text)
        self.assertRegex(text, r"(?m)^USER 10001:10001$")
        self.assertRegex(text, r"(?m)^STOPSIGNAL SIGINT$")
        self.assertRegex(text, r"(?m)^HEALTHCHECK ")

    def test_context_has_what_the_image_copies(self):
        ignore = read(".dockerignore")
        for need in ("LICENSE", "NOTICE", "requirements-lock.txt", "edgefdl/", "configs/demo_topology.yaml", "results/model.pt"):
            self.assertIn("!" + need, ignore)
            self.assertTrue((ROOT / need).exists(), need)


class GovernanceFilesTest(unittest.TestCase):
    def test_label_set(self):
        names = [x["name"] for x in labels.load()]
        for group in ("area: ci", "priority: high", "bug", "good first issue", "help wanted"):
            self.assertIn(group, names)
        cmd = labels.commands(labels.load())[0]
        self.assertEqual(cmd[:3], ["gh", "label", "create"])
        self.assertEqual(cmd[-1], "--force")

    def test_used_labels_exist(self):
        names = {x["name"] for x in labels.load()}
        used = set()
        for f in (ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml"):
            used |= set(yaml.safe_load(f.read_text(encoding="utf-8")).get("labels") or [])
        for u in yaml.safe_load(read(".github", "dependabot.yml"))["updates"]:
            used |= set(u["labels"])
        self.assertTrue(used)
        self.assertEqual(used - names, set())

    def test_workflows_pin_actions_by_commit(self):
        for f in (ROOT / ".github" / "workflows").glob("*.yml"):
            for use in re.findall(r"uses:\s*(\S+.*)$", f.read_text(encoding="utf-8"), re.M):
                self.assertRegex(use, r"@[0-9a-f]{40} # v\d", f.name)

    def test_codeql_is_guarded(self):
        wf = yaml.safe_load(read(".github", "workflows", "codeql.yml"))
        self.assertEqual(wf["jobs"]["analyze"]["needs"], "visibility")
        self.assertEqual(wf["jobs"]["analyze"]["if"], "needs.visibility.outputs.public == 'true'")

    def test_ci_steps_are_documented(self):
        text = read("tools", "dev", "ci.sh")
        steps = re.search(r"^known=\((.*)\)$", text, re.M).group(1).split()
        header = text[:text.index("set -euo pipefail")]
        for s in steps:
            self.assertRegex(header, rf"(?m)^#\s+{s}\s", s)


if __name__ == "__main__":
    unittest.main()
