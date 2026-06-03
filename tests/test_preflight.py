import os, subprocess, tempfile, unittest

SH = os.path.join(os.path.dirname(__file__), "..", "scripts", "preflight.sh")


def run(arg, cwd):
    return subprocess.run(["bash", SH, arg], cwd=cwd, capture_output=True, text=True)


class TestPreflight(unittest.TestCase):
    def test_resolves_explicit_path(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "myplan.md")
        open(p, "w").write("# x")
        r = run(p, d)
        self.assertIn("CIRCLE_PREFLIGHT: OK", r.stdout)
        self.assertIn(f"PLAN={p}", r.stdout)

    def test_resolves_by_name_glob(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "2026-06-03-superplan.md"), "w").write("# x")
        r = run("superplan", d)
        self.assertIn("CIRCLE_PREFLIGHT: OK", r.stdout)

    def test_error_when_not_found(self):
        d = tempfile.mkdtemp()
        r = run("nonexistent", d)
        self.assertIn("CIRCLE_PREFLIGHT: ERROR", r.stdout)
