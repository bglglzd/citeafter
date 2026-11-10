# Contributing

Open an issue with the policy schema version, supported runtime, minimal synthetic input, expected result and actual result. Remove credentials, private endpoints and customer material. Include both business and knowledge times for temporal issues.

Install the pinned development requirements and run the checks in [Operations](docs/operations.md#development). Keep the engine authoritative; UI code must not reimplement impact or release decisions. New semantics need independently specified expected results and failure recovery. Preserve original failing fixtures and report limitations. A changed evaluator or revealed dataset requires a new freeze before another blind evaluation.

Source is MIT licensed. Preserve third-party notices and required license files.
