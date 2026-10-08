# Credential and Restricted-Content Handling

Credentials are process inputs from environment variables or an external secret manager. They are never fields in task envelopes, artifacts, logs, screenshots, examples, or Git-tracked configuration.

Allowed logs contain provider name, non-sensitive base URL, model name, timing, response status, retry count, and configured/missing flags. They do not contain authorization headers, query parameters carrying tokens or email addresses, cookies, browser storage, SSO assertions, or raw authenticated HTML.

Article PDFs and authenticated pages are restricted runtime artifacts. Git stores only schemas, hashes where appropriate, sanitized fixtures, and minimum-necessary audit summaries.

If a credential is exposed, rotate or revoke it first, then use an approved history-rewrite process if necessary and verify remote caches and artifacts.
