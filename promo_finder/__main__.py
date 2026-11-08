"""CLI: python -m promo_finder --data data/broken-soldier-duke [--mode llm|rules|auto] [--out runs]"""
import argparse
import sys

from .pipeline import run


def main():
    ap = argparse.ArgumentParser(description="Find promo clips in microdrama screenplays")
    ap.add_argument("--data", required=True, help="folder of episode .txt screenplays")
    ap.add_argument("--out", default="runs", help="output root folder")
    ap.add_argument("--mode", default="auto", choices=["auto", "llm", "rules"])
    ap.add_argument("--show", default=None, help="show name (defaults to folder name)")
    a = ap.parse_args()

    def progress(step, frac, msg):
        print(f"[{step:>10}] {msg}", file=sys.stderr, flush=True)

    out = run(a.data, a.out, a.mode, progress, a.show)
    print(out)


if __name__ == "__main__":
    main()
