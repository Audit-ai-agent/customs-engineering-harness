# Local setup

1. Clone this harness and the four product repositories as sibling directories.
2. Enable the repository check hook once with `git config core.hooksPath .githooks`.
3. Keep credentials, datasets, runtime state, and generated evidence in the
   sibling `_local/` and `_artifacts/` directories; never copy them into this
   repository.
4. Run `harness/eng.ps1 doctor` before executing a repository check or UAT.
5. Use the product repository's own CI and release process for merges, tags, and
   deployment. The harness does not perform production certification.

The UAT commands require separately provisioned local credentials and data. A
missing local secret is an expected setup error, not a reason to add it to Git.
