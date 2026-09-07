from pathlib import Path

from .pipeline import run


def main() -> None:
    run(Path("data/work"), Path("public"), max_backtrack_days=7)


if __name__ == "__main__":
    main()
