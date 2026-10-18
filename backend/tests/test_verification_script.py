import sys

from scripts.verify_phases_1_3 import main


def test_live_verification_requires_explicit_readiness_confirmation(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(sys, "argv", ["verify_phases_1_3.py"])

    result = main()

    report = capsys.readouterr().out
    assert result == 1
    assert "--confirm-live" in report
    assert report.count("| FAIL |") == 3

