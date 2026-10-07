# Contributing

Keep changes scoped to the local Claude/ChatGPT adapter. Do not bundle proprietary clients, copied vendor system prompts, real conversations, account catalogs, runtime state, or credentials.

Install `.[dev]`, prepare the tokenizer cache, and run `python -m pytest -q` and `python scripts/check_public.py`. Tests must use temporary private state and synthetic accounts. External provider calls do not belong in the test suite. New source files require an intentional change to `PUBLIC_FILES.txt`; review every entry before adding it.

Include regression coverage for authentication, translation, routing or retry behavior that changes. Document compatibility limits honestly. Do not silently drop user context, bypass tool approvals, retry after tool activity, or switch billing providers on failure.

Before a release, build the wheel and source distribution, scan them with `--artifacts dist`, inspect dependency advisories, and confirm that a fresh environment can install and run the CLI. Review Git metadata as well as file contents. CI should run without repository secrets and with read-only permissions.
