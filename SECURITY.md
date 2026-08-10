# Security

- Do **not** commit `config.yaml`, `.env`, or real Cursor API keys.
- Create keys at https://cursor.com/dashboard/integrations and rotate if leaked.
- In Kubernetes, inject keys via Secrets (`cursor.existingSecret`); avoid putting keys in Helm `--set` when possible (they persist in release history).
- Report issues via GitHub Security Advisories or a private channel — do not open public issues that include live credentials.
