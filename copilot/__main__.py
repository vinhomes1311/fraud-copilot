import argparse
import json
import sys
from .contracts import ContractError
from .providers import MockProvider, OllamaProvider
from .retrieval import load_playbook
from .service import generate_brief


def main():
    parser = argparse.ArgumentParser(description="Generate a grounded investigation brief")
    parser.add_argument("--case", required=True)
    parser.add_argument("--playbook", required=True)
    parser.add_argument("--provider", choices=("mock", "ollama"), required=True)
    parser.add_argument("--allow-demo-playbook", action="store_true")
    args = parser.parse_args()
    try:
        with open(args.case, encoding="utf-8") as stream:
            case = json.load(stream)
        kb = load_playbook(args.playbook, args.allow_demo_playbook)
        provider = MockProvider() if args.provider == "mock" else OllamaProvider()
        result = generate_brief(case, kb, provider)
    except (ValueError, OSError, ContractError):
        print("Invalid input or configuration; check README and input contract.", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
