"""Run the manual-revisit choice and stale-state UI contract in a Node DOM harness."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_manual_revisit_dom_binding_and_authority():
    completed = subprocess.run(
        [
            "node",
            str(ROOT / "tests" / "product_console_manual_revisit_dom.js"),
            str(ROOT / "el" / "product" / "static" / "product_console.html"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
