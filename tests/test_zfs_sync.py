import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
zfs_sync = SourceFileLoader("zfs_sync", str(ROOT / "zfs-sync")).load_module()


class PlanStreamsTests(unittest.TestCase):
    def test_new_dataset_sends_full_then_chain(self):
        plan = zfs_sync.plan_streams([], ["s1", "s2", "s3"])
        self.assertEqual(plan, [("full", "s1"), ("incr", "s1", "s2"), ("incr", "s2", "s3")])

    def test_up_to_date_returns_empty_plan(self):
        plan = zfs_sync.plan_streams(["s1", "s2"], ["s1", "s2"])
        self.assertEqual(plan, [])

    def test_incremental_from_newest_common(self):
        plan = zfs_sync.plan_streams(["s1", "s2", "s3"], ["s1", "s2", "s3", "s4", "s5"])
        self.assertEqual(plan, [("incr", "s3", "s4"), ("incr", "s4", "s5")])

    def test_common_snapshot_missing_in_local_falls_back_to_earliest(self):
        plan = zfs_sync.plan_streams(["s1", "s3"], ["s1", "s2", "s3", "s4"])
        self.assertEqual(plan, [("incr", "s3", "s4")])

    def test_no_common_snapshot_sends_latest_full(self):
        plan = zfs_sync.plan_streams(["x1", "x2"], ["s1", "s2", "s3"])
        self.assertEqual(plan, [("full", "s3")])

    def test_no_remote_snapshots(self):
        self.assertEqual(zfs_sync.plan_streams(["s1"], []), [])

    def test_local_only_snapshots_are_ignored(self):
        plan = zfs_sync.plan_streams(["s1", "local-only"], ["s1"])
        self.assertEqual(plan, [])


class ConfigTests(unittest.TestCase):
    def test_example_config(self):
        cfg = zfs_sync.load_config(str(ROOT / "zfs-sync.conf.example"))
        self.assertEqual(cfg.host, "truenas.example.com")
        self.assertEqual(cfg.port, 22)
        self.assertEqual(cfg.user, "root")
        self.assertEqual(cfg.identity_file, "/root/.ssh/zfs-sync_ed25519")
        self.assertEqual(cfg.command_prefix, "sudo -n")
        self.assertFalse(cfg.strict_host_key_checking)
        self.assertEqual(cfg.connect_timeout, 10)
        self.assertEqual(cfg.mbuffer, "/usr/bin/mbuffer")
        self.assertEqual(cfg.mbuffer_size, "256M")
        self.assertFalse(cfg.compression)
        self.assertEqual(cfg.retries, 2)
        self.assertEqual(cfg.retry_delay, 15)
        self.assertEqual(cfg.recv_flags, ["-Fs"])
        self.assertEqual(cfg.full_send_flags, [])
        self.assertEqual(cfg.incr_send_flags, [])
        self.assertFalse(cfg.include_children)
        self.assertTrue(cfg.tag_local_source)
        self.assertTrue(cfg.use_resume_token)
        self.assertEqual(cfg.state_dir, "/var/lib/zfs-sync")
        self.assertEqual(
            cfg.mappings,
            {
                "tank-10-0/vm1": "tank/vmimages",
                "tank-10-0/db": "tank/databases",
                "tank-10-0/media": "tank/media",
            },
        )
        self.assertEqual(list(cfg.mappings), ["tank-10-0/vm1", "tank-10-0/db", "tank-10-0/media"])

    def test_missing_file(self):
        with self.assertRaises(zfs_sync.ConfigError):
            zfs_sync.load_config("/nonexistent/zfs-sync.conf")

    def test_missing_host(self):
        self._expect_error("[zfs]\n[mappings]\na/b = c/d\n", "host is required")

    def test_missing_mappings_section(self):
        self._expect_error("[remote]\nhost = h\n", "missing the [mappings] section")

    def test_invalid_boolean(self):
        self._expect_error("[remote]\nhost = h\nstrict_host_key_checking = maybe\n[mappings]\na/b = c/d\n", "not a boolean")

    def test_invalid_dataset_name(self):
        self._expect_error("[remote]\nhost = h\n[mappings]\na/b@snap = c/d\n", "invalid local dataset")

    def test_local_remote_must_differ(self):
        self._expect_error("[remote]\nhost = h\n[mappings]\ntank/a = tank/a\n", "must differ")

    def test_invalid_mbuffer_size(self):
        self._expect_error(
            "[remote]\nhost = h\n[transfer]\nmbuffer_size = huge\n[mappings]\na/b = c/d\n",
            "not a valid size",
        )

    def _expect_error(self, body, fragment):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as handle:
            handle.write(body)
            path = handle.name
        try:
            with self.assertRaises(zfs_sync.ConfigError) as ctx:
                zfs_sync.load_config(path)
            self.assertIn(fragment, str(ctx.exception))
        finally:
            os.unlink(path)


class CommandBuildTests(unittest.TestCase):
    def setUp(self):
        self.cfg = zfs_sync.load_config(str(ROOT / "zfs-sync.conf.example"))

    def test_ssh_argv_core_options(self):
        argv = zfs_sync.build_ssh_argv(self.cfg, "zfs list")
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[argv.index("-p") + 1], "22")
        self.assertEqual(argv[argv.index("-i") + 1], "/root/.ssh/zfs-sync_ed25519")
        self.assertIn("StrictHostKeyChecking=no", argv)
        self.assertIn("BatchMode=yes", " ".join(argv))
        self.assertNotIn("-C", argv)
        self.assertEqual(argv[-2], "root@truenas.example.com")
        self.assertEqual(argv[-1], "zfs list")

    def test_compression_enabled(self):
        self.cfg.compression = True
        self.assertIn("-C", zfs_sync.build_ssh_argv(self.cfg, "x"))

    def test_command_prefix_is_prepended(self):
        command = zfs_sync.remote_zfs_command(self.cfg, ["list", "-H", "tank/a"])
        self.assertEqual(command, "sudo -n zfs list -H tank/a")

    def test_snapshot_names_are_quoted(self):
        command = zfs_sync.remote_zfs_command(self.cfg, ["send", "tank/a@snap-1"])
        self.assertEqual(command, "sudo -n zfs send tank/a@snap-1")

    def test_pipeline_adds_mbuffer(self):
        pipeline = zfs_sync.remote_pipeline(self.cfg, ["send", "tank/a@s"])
        self.assertEqual(pipeline, "sudo -n zfs send tank/a@s | /usr/bin/mbuffer -m 256M -q")

    def test_pipeline_without_mbuffer(self):
        self.cfg.mbuffer = ""
        pipeline = zfs_sync.remote_pipeline(self.cfg, ["send", "tank/a@s"])
        self.assertEqual(pipeline, "sudo -n zfs send tank/a@s")

    def test_incremental_send_args_join_correctly(self):
        args = ["send", "-i", "tank/vmimages@s1", "tank/vmimages@s2"]
        self.assertEqual(
            zfs_sync.remote_zfs_command(self.cfg, args),
            "sudo -n zfs send -i tank/vmimages@s1 tank/vmimages@s2",
        )


class SnapshotListingTests(unittest.TestCase):
    def test_parse_snapshot_names_ignores_stray_lines(self):
        output = "tank/vmimages@s1\ntank/vmimages@s2\nbad line\n"
        self.assertEqual(zfs_sync._parse_snapshot_names(output, "tank/vmimages"), ["s1", "s2"])

    def test_parse_snapshot_names_excludes_children(self):
        output = "tank/vmimages@s1\ntank/vmimages/child@s1\n"
        self.assertEqual(zfs_sync._parse_snapshot_names(output, "tank/vmimages"), ["s1"])


if __name__ == "__main__":
    unittest.main()
