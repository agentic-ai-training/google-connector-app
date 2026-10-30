"""Public scanner acquisition/verification; defaults to read-only content probing."""
import argparse
import json
from pathlib import Path

from app.coding import runtime_scanner_assets as scanner, runtime_base_acquisition as cas


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--acquire", action="store_true")
    action.add_argument("--verify-signature", action="store_true")
    parser.add_argument("--verifier-python", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / ".gca-local/runtime-scanner-assets-v1"
    cas.protected(root.parent)
    if args.acquire:
        root.mkdir(mode=0o700, exist_ok=True)
        result = scanner.acquire_assets(root)
    elif args.verify_signature:
        if args.verifier_python is None:
            parser.error("an isolated operator verifier environment is required")
        result = scanner.verify_signature(root, args.verifier_python)
    else:
        result = scanner.observe_assets(root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
