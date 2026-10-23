import sys


def _main() -> int:
    try:
        from oncall.cli import main
    except ImportError:
        print("oncall.cli is not available yet (Person 5 owns it).", file=sys.stderr)
        return 2
    return int(main() or 0)


if __name__ == "__main__":
    sys.exit(_main())
