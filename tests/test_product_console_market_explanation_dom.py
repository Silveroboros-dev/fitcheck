"""Exercise market-card explanations in a disposable Node DOM harness."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MINIMUM_NODE_MAJOR = 22


def test_market_explanations_render_plain_language_comparison_cards():
    version = subprocess.run(
        ["node", "--version"], check=False, capture_output=True, text=True
    )
    assert version.returncode == 0, version.stderr
    major = int(version.stdout.strip().removeprefix("v").split(".", 1)[0])
    assert major >= MINIMUM_NODE_MAJOR
    completed = subprocess.run(
        [
            "node",
            str(ROOT / "tests" / "product_console_market_explanation_dom.js"),
            str(ROOT / "el" / "product" / "static" / "product_console.html"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
