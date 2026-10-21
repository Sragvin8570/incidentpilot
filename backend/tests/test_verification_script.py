import sys

from scripts.verify_phases_1_3 import main
from scripts.verify_phase_8 import main as phase_8_main


def test_live_verification_requires_explicit_readiness_confirmation(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(sys, "argv", ["verify_phases_1_3.py"])

    result = main()

    report = capsys.readouterr().out
    assert result == 1
    assert "--confirm-live" in report
    assert report.count("| FAIL |") == 3


def test_phase_8_live_verification_requires_explicit_confirmation(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(sys, "argv", ["verify_phase_8.py"])

    result = phase_8_main()

    report = capsys.readouterr().out
    assert result == 1
    assert "--confirm-live" in report
    assert report.count("| FAIL |") == 2
