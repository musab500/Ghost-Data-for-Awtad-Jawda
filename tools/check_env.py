"""Show which LLM provider and models WILL be used. Never prints a key.

Prints only "key present: yes/no" per provider plus provider and model names. The key itself is
never printed, logged or derived into any output. Reads the environment and the gitignored .env.

    python3 tools/check_env.py            # configuration only, no network
    python3 tools/check_env.py --ping     # also makes one minimal real call per model (a few tokens)
                                          # to confirm each model name exists and answers
    python3 tools/check_env.py --list     # print the Flash model names your key can see (Gemini),
                                          # to pick CLASSIFIER_MODEL / VISION_MODEL
Exit code: 0 ready, 1 no provider configured or a ping failed.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "code"))

import llm_provider as lp  # noqa: E402

env_file = lp.load_dotenv()
d = lp.describe()
yn = lambda b: "yes" if b else "no"  # noqa: E731
print(f".env file found: {yn(env_file)}")
print(f"gemini key present: {yn(d['key_present']['gemini'])}")
print(f"anthropic key present: {yn(d['key_present']['anthropic'])}")
if d["provider"] == "none":
    print("provider: none -> FALLBACK mode (deterministic classifier, no model calls)")
    print("set GEMINI_API_KEY (or ANTHROPIC_API_KEY) in .env, or LLM_PROVIDER to choose")
    sys.exit(1)
print(f"provider: {d['provider']}")
print(f"classifier model: {d['classifier_model']}")
print(f"vision model: {d['vision_model']}")
if "--list" in sys.argv:
    try:
        names = lp.list_models("flash")
    except lp.LLMFailure as f:
        print(f"list failed: {f}")
        sys.exit(1)
    print(f"{len(names)} flash model(s) visible to this key:")
    print("\n".join("  " + n for n in names))
    sys.exit(0)
if "--ping" not in sys.argv:
    print("model names are not confirmed until you run: python3 tools/check_env.py --ping")
    sys.exit(0)
failed = False
for role in ("classifier", "vision"):
    ok, detail, _ = lp.ping(role)
    print(f"ping {role}: {'OK' if ok else 'FAILED'} - {detail}")
    failed |= not ok
sys.exit(1 if failed else 0)
