"""Evaluate the translation validators (free) and optionally the LLM (paid) on the reviewed corpus.

  python3 tools/eval_translation.py                  # validators vs. existing human-reviewed translations
  python3 tools/eval_translation.py --llm 20 --yes   # also translate 20 posts with the configured model

The corpus translations were reviewed by a human, so a high pass rate means the validators do not reject
good work; failures are listed so they can be inspected (a failure is not automatically a validator bug).
With --llm the model output goes through the same validators and is compared by pass rate only.
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import auto, llm, store, translate  # noqa: E402
from pipeline.validate import check_translation  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", type=int, default=0, help="translate N sample posts with the configured model")
    ap.add_argument("--yes", action="store_true", help="confirm the paid LLM calls")
    ap.add_argument("--show", type=int, default=15, help="how many failures to print")
    args = ap.parse_args()

    gloss = translate.load_glossary()
    posts = [p for p in store.iter_posts() if p.get("relevant", True) and p.get("content_en") and p.get("content_original")]
    fails = [(p, check_translation(p["content_original"], p["content_en"], gloss["keep"])) for p in posts]
    fails = [(p, i) for p, i in fails if i]
    print(f"corpus: {len(posts)} reviewed translations, {len(fails)} flagged by validators "
          f"({100 * len(fails) / max(1, len(posts)):.1f}%)")
    for p, issues in fails[:args.show]:
        print(f"- {p['id']}: {'; '.join(issues)[:160]}")

    if args.llm:
        if not args.yes:
            print("--llm needs --yes (paid calls)")
            return 1
        acfg, env = auto.load_automation(), auto.build_env()
        client = llm.OpenRouter(env.get("OPENROUTER_API_KEY"), acfg["llm"])
        model, note = client.resolve_model()
        sample = random.Random(7).sample(posts, min(args.llm, len(posts)))
        items = [{"id": p["id"], "text": p["content_original"], "handle": p["account_handle"], "brands": ["XMTrading"]}
                 for p in sample]
        out = translate.translate_items(client, model, items, gloss, acfg["llm"])
        print(f"model {model}: {len(out['ok'])}/{len(items)} passed validation, {len(out['held'])} held, "
              f"${out['cost_usd']:.3f}")
        for pid, problems in list(out["held"].items())[:args.show]:
            print(f"- held {pid}: {'; '.join(problems)[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
