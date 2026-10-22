"""Exercise the registration helper boundary without starting Codex."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

from scripts import fork_migration_probe as probe


class RegistrationReadbackTests(unittest.TestCase):
    def setUp(self):
        self.registration = {
            "source_type": "git",
            "source": "https://github.com/example/marketplace.git",
            "ref": "main",
            "last_revision": "a" * 40,
        }
        self.user = {"marketplaces": {"fixture": dict(self.registration)}}
        self.effective = copy.deepcopy(self.user)

    def run_helper(self, *, version="user-layer-version"):
        """Run the shipped helper string against an entirely in-memory client."""
        module = types.ModuleType("configure_native_routing")
        app = mock.MagicMock()
        app.__enter__.return_value = app
        read = {"config": self.effective}
        app.request.return_value = read
        module.AppServer = mock.Mock(return_value=app)
        module._user_layer = mock.Mock(return_value=(self.user, version))

        def nested_get(config, *keys):
            for key in keys:
                if not isinstance(config, dict):
                    return None
                config = config.get(key)
            return config

        module.nested_get = nested_get
        output = io.StringIO()
        argv = ["registration_read.py", "/unused/module", "/unused/codex",
                "/unused/home", "fixture"]
        error = None
        with mock.patch.dict(sys.modules, {"configure_native_routing": module}), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(sys, "path", list(sys.path)), \
                redirect_stdout(output):
            try:
                exec(compile(probe.REGISTRATION_HELPER, "registration_read.py", "exec"), {})
            except RuntimeError as exc:
                error = exc
        module.AppServer.assert_called_once_with(Path("/unused/codex"), Path("/unused/home"))
        app.request.assert_called_once_with(
            "config/read", {"includeLayers": True, "cwd": str(Path.cwd())})
        module._user_layer.assert_called_once_with(read)
        return output.getvalue(), error

    def test_only_four_registration_fields_are_emitted(self):
        for config in (self.user, self.effective):
            config["api_key"] = "UNRELATED_SECRET"
            config["marketplaces"]["fixture"]["unknown_auth"] = "REGISTRATION_SECRET"
            config["marketplaces"]["other"] = {"password": "OTHER_SECRET"}
        output, error = self.run_helper()
        self.assertIsNone(error)
        self.assertEqual(json.loads(output), self.registration)
        self.assertEqual(len(output.splitlines()), 1)
        self.assertNotIn("SECRET", output)

    def test_effective_user_mismatch_is_rejected_without_output(self):
        for field in self.registration:
            with self.subTest(field=field):
                self.effective = copy.deepcopy(self.user)
                self.effective["marketplaces"]["fixture"][field] = "OVERRIDE_SECRET"
                output, error = self.run_helper()
                self.assertEqual(output, "")
                self.assertRegex(str(error), "effective marketplace differs")
                self.assertNotIn("OVERRIDE_SECRET", str(error))

    def test_absent_user_version_is_rejected_without_output(self):
        for version in (None, ""):
            with self.subTest(version=version):
                output, error = self.run_helper(version=version)
                self.assertEqual(output, "")
                self.assertRegex(str(error), "user registration layer unavailable")

    def test_missing_or_invalid_marketplace_is_rejected_without_output(self):
        for layer in ("user", "effective"):
            for value in ({}, {"marketplaces": {}},
                          {"marketplaces": {"fixture": "CONFIG_SECRET"}}):
                with self.subTest(layer=layer, value=value):
                    self.user = {"marketplaces": {"fixture": dict(self.registration)}}
                    self.effective = copy.deepcopy(self.user)
                    setattr(self, layer, value)
                    output, error = self.run_helper()
                    self.assertEqual(output, "")
                    self.assertRegex(str(error), "marketplace registration unavailable")
                    self.assertNotIn("CONFIG_SECRET", str(error))


if __name__ == "__main__":
    unittest.main()
