# Security and privacy boundary

Credentials are supplied through environment variables or an external secret manager. Never put keys, signed URLs, cookies, passwords, personal contact details, or private library paths in source, manifests, prompts, or command-line arguments.

The public repository contains a reviewed `harness/` source tree, not the entire private research workspace. Source archives are produced by `scripts/release_bundle.py`. The exporter excludes Git history, historical handoff documents, raw runs, databases, PDFs, browser/session state, and local configuration. It strips archive user/group ownership and reports only file names and finding categories during audits. Scientific author names and source provenance are research metadata, not developer identity.

The scanner is a release gate, not proof that arbitrary personal information has been identified. Review the resulting archive before publication. If a real credential has ever been pushed, rotate or revoke it even after deleting it from the current tree. Public Git history requires a separate, explicitly approved cleanup or a new clean repository; this release does not rewrite existing history.

Authorized HTTPS acquisition is opt-in, host-allowlisted, public-address-only, credential-free, redirect-free, and size-limited. Institutional logins, MFA, CAPTCHA, paywalls, and signed download links require an operator-managed route. Enforce outbound network allowlists at the deployment layer as defense in depth against DNS changes.

Runtime state and evidence are private, persistent data. Encrypt the host volume, limit filesystem access, back up SQLite using the online backup API, and keep raw runtime logs out of publication artifacts. Workflow logs redact recognizable tokens and secret environment values. Do not expose the worker or database as a public HTTP service.
