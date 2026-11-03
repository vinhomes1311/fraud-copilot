import argparse
import json
import sys
from pathlib import Path
from ..providers import MockProvider, OllamaProvider
from .knowledge import load
from .vectors import OllamaEmbeddings, SemanticRetriever, FixtureRetriever, build
from .service import generate


def main():
    p = argparse.ArgumentParser(description="Architecture v2 LLM/RAG module")
    p.add_argument("command", choices=("index", "brief"))
    p.add_argument("--case")
    p.add_argument("--playbook", default="knowledge/common_playbook.json")
    p.add_argument("--index", default="artifacts/playbook-index.json")
    p.add_argument("--provider", choices=("mock", "ollama"), default="ollama")
    p.add_argument("--retriever", choices=("fixture", "semantic"), default="semantic")
    p.add_argument("--candidate-count", type=int, choices=range(1,6), default=5)
    p.add_argument("--output")
    args = p.parse_args()
    try:
        kb = load(args.playbook)
        if args.command == "index":
            index = build(kb, OllamaEmbeddings(), args.index)
            print(json.dumps({"status": "indexed", "chunks": len(index["ids"]), "model": index["embedding_model"]}))
            return 0
        if not args.case:
            p.error("brief requires --case")
        with open(args.case, encoding="utf-8") as f:
            case = json.load(f)
        provider = MockProvider() if args.provider == "mock" else OllamaProvider(timeout=60)
        retriever = FixtureRetriever(kb, args.candidate_count) if args.retriever == "fixture" else SemanticRetriever(kb, OllamaEmbeddings(), args.index, args.candidate_count)
        result = generate(case, kb, provider, retriever)
        output = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(output, encoding="utf-8")
        print(output)
        return 0 if result["brief_status"] == "COMPLETED" else 1
    except Exception as exc:
        print(f"Configuration/input/service failure ({type(exc).__name__}); check model, index and contract. No payload logged.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
