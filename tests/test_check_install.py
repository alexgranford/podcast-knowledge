"""The monitor deployment check must detect both source drift and a wrong cron path."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_install


def test_monitor_deployment_parity(tmp_path):
    source, installed = tmp_path / "source", tmp_path / "installed"
    for relative in check_install.MONITOR_FILES:
        for root in (source, installed):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("same")
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(str(installed / "scripts/podmonitor.py"))
    assert check_install.verify(source, installed, wrapper) == []
    (installed / "scripts/podmonitor.py").write_text("old")
    assert "mismatch scripts/podmonitor.py" in check_install.verify(source, installed, wrapper)[0]
    (installed / "scripts/podmonitor.py").write_text("same")
    wrapper.write_text("/wrong/monitor.py")
    assert "cron wrapper does not point" in check_install.verify(source, installed, wrapper)[0]
