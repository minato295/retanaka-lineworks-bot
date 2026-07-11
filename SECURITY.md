# Security

## Secrets

Never commit:

- LINE WORKS Incoming Webhook URLs
- ScreenshotOne API keys
- Xserver API keys or FTPS passwords
- LINE legacy channel tokens or group IDs
- email addresses used for alerts
- production config or state files

Use config.example.json and .retanaka.env.example as templates only.

## Xserver layout

Store executable code, config, logs, and state outside public_html. The public image directory is intentionally internet-accessible and must contain only generated screenshots of public price information.

Recommended permissions:

- private directories: 700
- private config, state, and logs: 600
- executable scripts: 700
- public image directory: 755
- public screenshots: 644

## Reporting

Do not open a public issue containing credentials or production identifiers. Revoke exposed webhook URLs and API keys before reporting an incident.
