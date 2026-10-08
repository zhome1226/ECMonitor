# Download Specialist

## Responsibility

Download Specialist consumes versioned requests, checks existing lawful local inventory, tries configured authorized routes in order, validates content, deduplicates artifacts, and emits a durable result.

## Required route order

1. Existing local library or prior validated artifact.
2. Lawful open-access location.
3. Publisher identity resolution.
4. Authorized publisher API.
5. Institution-authenticated browser session completed by the user.
6. Terminal blocked or skip status.

## Safety boundary

The agent never automates credentials, MFA, CAPTCHA, SSO, or entitlement decisions. HTTP status and content type are insufficient: PDF signature, file size, readability, page count, and checksum must be evaluated. HTML login/challenge pages, truncated files, and publisher previews are rejected.

The repository supplies the route protocol, bounded worker, and validator. Deployment-specific OA, publisher API, browser, and inventory plugins are intentionally not hard-coded here.

- Code: `src/ecmonitor/download_specialist/`
- Config: `configs/download/download_specialist_v1.yaml`
- Schemas: `schemas/handoff/` and `schemas/download/`
