"""The monitor deployment check must detect source drift, a wrapper that runs another monitor, and a
wrapper other than the one the checkout ships."""
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
    wrapper = tmp_path / "wrapper.py"                    # its dry start names the monitor it would run
    wrapper.write_text(f"print({str(installed / 'scripts/podmonitor.py')!r} + ' on python')\n")
    assert check_install.verify(source, installed, wrapper) == []
    (installed / "scripts/podmonitor.py").write_text("old")
    assert "mismatch scripts/podmonitor.py" in check_install.verify(source, installed, wrapper)[0]
    (installed / "scripts/podmonitor.py").write_text("same")
    shipped = source / "hermes" / "wrapper.py"
    shipped.parent.mkdir()
    shipped.write_text("another wrapper\n")
    assert "differs from this checkout's hermes/wrapper.py" in check_install.verify(source, installed, wrapper)[0]
    wrapper.write_text("print('/wrong/monitor.py on python')\n")
    assert "cron wrapper does not run the installed monitor" in check_install.verify(source, installed, wrapper)[0]
