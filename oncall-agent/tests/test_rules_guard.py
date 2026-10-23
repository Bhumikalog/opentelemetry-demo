import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "oncall"

FORBIDDEN = [
    r"flagd",
    r"flag_config",
    r"feature/api",
    r"FLAGS_READ",
    r"faultctl",
]


def test_agent_code_never_reads_feature_flags_or_touches_faultctl():
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(PACKAGE.parent)}:{line} matches {pattern!r}")
    assert not offenders, "Hackathon rule 6 (disqualification):\n" + "\n".join(offenders)
