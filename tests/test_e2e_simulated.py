"""End-to-end simulation of zfs-sync against the ./fake-zfs simulator.

No real ZFS pool is touched: fake zfs and ssh binaries are put first on PATH
(fake zfs is backed by a JSON store, fake ssh runs the remote command via
sh -c), and the tool is exercised as a real subprocess. Covers: full import,
idempotence, incremental chain, resume token after an interrupted receive,
include_children, tag mismatch refusal, and the no-common-snapshot full
replace.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
TOOL = os.path.join(PROJECT, "zfs-sync")
FAKE_ZFS_SRC = os.path.join(HERE, "fake-zfs")


class SimulatedEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zfs-sync-sim-")
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        self.bin = os.path.join(bindir, "zfs")
        shutil.copyfile(FAKE_ZFS_SRC, self.bin)
        os.chmod(self.bin, 0o755)
        fake_ssh = os.path.join(bindir, "ssh")
        shutil.copyfile(os.path.join(HERE, "fake-ssh"), fake_ssh)
        os.chmod(fake_ssh, 0o755)
        self.state = os.path.join(self.tmp, "state")
        self.store_path = os.path.join(self.tmp, "store.json")
        self.env = os.environ.copy()
        self.env["PATH"] = bindir + os.pathsep + self.env["PATH"]
        self.env["FAKE_ZFS_STORE"] = self.store_path

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, mappings, extra_zfs=""):
        path = os.path.join(self.tmp, "zfs-sync.conf")
        body = [
            "[remote]",
            "host = fake.example",
            "user = root",
            "",
            "[transfer]",
            "mbuffer =",
            "retries = 0",
            "",
            "[zfs]",
            extra_zfs.rstrip("\n"),
            f"state_dir = {self.state}",
            "",
            "[mappings]",
            *[f"{local} = {remote}" for local, remote in mappings.items()],
            "",
        ]
        with open(path, "w") as fh:
            fh.write("\n".join(body))
        return path

    def write_store(self, datasets, interrupt=None):
        data = {"datasets": datasets, "resume_tokens": {}}
        if interrupt:
            data["interrupt"] = interrupt
        with open(self.store_path, "w") as fh:
            json.dump(data, fh)

    def store(self):
        with open(self.store_path) as fh:
            return json.load(fh)

    def run_tool(self, config, *datasets):
        return subprocess.run(
            [sys.executable, TOOL, "-c", config, *datasets],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def snaps(self, name):
        entry = self.store()["datasets"].get(name, {"snaps": []})
        return {s["name"]: s["content"] for s in entry["snaps"]}

    def props(self, name):
        return self.store()["datasets"].get(name, {"props": {}})["props"]

    def status(self, local):
        path = os.path.join(self.state, local.replace("/", "__") + ".json")
        with open(path) as fh:
            return json.load(fh)

    @staticmethod
    def snap_list(pairs):
        return [
            {"name": name, "creation": index, "content": content}
            for index, (name, content) in enumerate(pairs.items(), start=1)
        ]

    @staticmethod
    def dset(snaps_=(), props_=()):
        return {"snaps": list(snaps_), "props": dict(props_)}

    def test_full_import_chain_idempotence_and_resume(self):
        config = self.write_config({"tank-10-0/vm1": "tank/vmimages"})
        self.write_store({
            "tank": self.dset(),
            "tank/vmimages": self.dset(self.snap_list({"s1": "c1", "s2": "c2", "s3": "c3"})),
            "tank-10-0": self.dset(),
        })

        result = self.run_tool(config, "tank-10-0/vm1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snaps("tank-10-0/vm1"), {"s1": "c1", "s2": "c2", "s3": "c3"})
        self.assertEqual(self.props("tank-10-0/vm1").get("zfs-sync:source"), "tank/vmimages")
        self.assertEqual(self.status("tank-10-0/vm1")["result"], "ok")

        result = self.run_tool(config, "tank-10-0/vm1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.status("tank-10-0/vm1")["detail"], "up to date")

        current = self.store()["datasets"]
        current["tank/vmimages"] = self.dset(self.snap_list({"s1": "c1", "s2": "c2", "s3": "c3", "s4": "c4"}))
        self.write_store(current)
        result = self.run_tool(config, "tank-10-0/vm1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snaps("tank-10-0/vm1").get("s4"), "c4")
        self.assertEqual(self.status("tank-10-0/vm1")["detail"], "1 stream(s) sent")

        current = self.store()["datasets"]
        current["tank/vmimages"] = self.dset(self.snap_list(
            {"s1": "c1", "s2": "c2", "s3": "c3", "s4": "c4", "s5": "c5"}))
        self.write_store(current, interrupt="s5")
        result = self.run_tool(config, "tank-10-0/vm1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.status("tank-10-0/vm1")["result"], "error")
        self.assertNotIn("s5", self.snaps("tank-10-0/vm1"))
        self.assertIn("tank-10-0/vm1", self.store().get("resume_tokens", {}))

        result = self.run_tool(config, "tank-10-0/vm1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snaps("tank-10-0/vm1").get("s5"), "c5")
        self.assertFalse(self.store().get("resume_tokens"))
        self.assertEqual(self.status("tank-10-0/vm1")["result"], "ok")

    def test_include_children(self):
        config = self.write_config({"tank-10-0/data": "tank/data"}, extra_zfs="include_children = yes")
        self.write_store({
            "tank": self.dset(),
            "tank/data": self.dset(self.snap_list({"d1": "p1"})),
            "tank/data/a": self.dset(self.snap_list({"d1": "a1"})),
            "tank/data/b": self.dset(self.snap_list({"d1": "b1"})),
            "tank-10-0": self.dset(),
        })
        result = self.run_tool(config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snaps("tank-10-0/data"), {"d1": "p1"})
        self.assertEqual(self.snaps("tank-10-0/data/a"), {"d1": "a1"})
        self.assertEqual(self.snaps("tank-10-0/data/b"), {"d1": "b1"})
        self.assertEqual(self.props("tank-10-0/data/a").get("zfs-sync:source"), "tank/data/a")

    def test_tag_mismatch_refused(self):
        config = self.write_config({"tank-10-0/tm": "tank/tm"})
        self.write_store({
            "tank": self.dset(),
            "tank/tm": self.dset(self.snap_list({"t0": "x"})),
            "tank-10-0": self.dset(),
            "tank-10-0/tm": self.dset(self.snap_list({"t0": "x"}), {"zfs-sync:source": "tank/other"}),
        })
        result = self.run_tool(config, "tank-10-0/tm")
        self.assertEqual(result.returncode, 1)
        self.assertIn("different source", self.status("tank-10-0/tm")["detail"])

    def test_no_common_snapshot_replaces_local_history(self):
        config = self.write_config({"tank-10-0/nc": "tank/nc"})
        self.write_store({
            "tank": self.dset(),
            "tank/nc": self.dset(self.snap_list({"n1": "n1c", "n2": "n2c"})),
            "tank-10-0": self.dset(),
            "tank-10-0/nc": self.dset(self.snap_list({"old": "zz"})),
        })
        result = self.run_tool(config, "tank-10-0/nc")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snaps("tank-10-0/nc"), {"n2": "n2c"})


if __name__ == "__main__":
    unittest.main()
