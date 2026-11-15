import sys
from unittest import mock, skipIf

from django.test import SimpleTestCase

# gunicorn is not available on Windows
if sys.platform != "win32":
    from gunicorn.app.wsgiapp import WSGIApplication, run


@skipIf(sys.platform == "win32", "gunicorn is not fully supported on Windows")
class GunicornConfigTests(SimpleTestCase):
    """Test Gunicorn configuration."""

    def test_config(self):
        """Test that the Gunicorn configuration file is valid."""
        argv = [
            "gunicorn",
            "--check-config",
            "--config",
            "python:config.gunicorn",
            "config.wsgi",
        ]
        mock_argv = mock.patch.object(sys, "argv", argv)

        with self.assertRaises(SystemExit) as cm, mock_argv:
            run()

        exit_code = cm.exception.args[0]
        self.assertEqual(exit_code, 0)

    def test_threads(self):
        """Test that the config runs 4 threads, which makes gunicorn use gthread."""
        argv = ["gunicorn", "--config", "python:config.gunicorn", "config.wsgi"]

        with mock.patch.object(sys, "argv", argv):
            cfg = WSGIApplication().cfg

        self.assertEqual(cfg.threads, 4)
        self.assertEqual(cfg.worker_class.__name__, "ThreadWorker")
