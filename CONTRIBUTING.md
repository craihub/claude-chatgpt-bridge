# Contributing

Keep changes scoped to the local Claude/ChatGPT adapter. Do not bundle proprietary clients, copied vendor system prompts, real conversations, account catalogs, runtime state, or credentials.

Install `.[dev]`, prepare the tokenizer cache, and run `python -m pytest -q` and `python scripts/check_public.py`. Tests must use temporary private state and synthetic accounts. External provider calls do not belong in the test suite. New source files require an intentional change to `PUBLIC_FILES.txt`; review every entry before adding it.

Include regression coverage for authentication, translation, routing or retry behavior that changes. Document compatibility limits honestly. Do not silently drop user context, bypass tool approvals, retry after tool activity, or switch billing providers on failure.

Desktop changes must preserve existing provider profiles, remain resumable and support guarded undo. Test generated service definitions, account/config verification invalidation and credential isolation with synthetic data. Validate native ACLs and service lifecycle on each target OS before claiming support. A terminal response is not a desktop acceptance test. Do not use live accounts or install services into a contributor's ordinary profile during automated tests.

Before a release, build the wheel and source distribution, scan them with `--artifacts dist`, inspect dependency advisories, and confirm that a fresh environment can install and run the CLI. Review Git metadata as well as file contents. CI should run without repository secrets and with read-only permissions.

Pull-request CI checks out the exact proposed head commit, not GitHub's temporary merge commit, whose generated author metadata can contain account identity absent from the submitted source history. The history scanner remains strict. Check base-branch changes and merge conflicts separately before merging; head-commit CI does not test a synthesized merge with a newer base.
