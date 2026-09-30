# Security and privacy

## Intended trust boundary

This is a local, single-user notification utility. It executes code from its installed runtime and trusts its local configuration. A process running as the same user can modify that runtime, inspect environment variables/private files, invoke the sender, or submit fake events. File modes do not defend against the same account, an administrator, or a compromised host.

The installation is scoped to the account and the agent configuration directories selected during installation. Do not place its private directory inside a shared repository, world-writable directory, or synced public folder. Symlink configuration files are refused rather than followed; this is not a complete defense against all filesystem races or symlinked ancestor directories.

## Outbound information

The only runtime network client posts to the fixed Pushover HTTPS endpoints for user validation and sending messages. TLS verification is enabled; redirects are refused so credentials cannot be forwarded to a new destination. Standard Python/system proxy configuration may still route HTTPS through your organization’s network.

The default message includes category/summary, project basename, hostname, a short session ID, and source event. It does not include raw prompts, tool inputs, assistant responses, transcript files, or terminal output. Host/project metadata can also be disabled. A short session ID remains for correlation.

Enabling `privacy.include_details` adds selected tool/question/error text. Known Pushover credentials, common key formats, bearer tokens, and some key/value secrets are redacted, but arbitrary confidential content can remain. Do not enable details for sessions containing sensitive information without assessing that disclosure. Pushover and the destination devices receive the message. Lock-screen previews can expose it.

## Local retention

Credentials are unencrypted JSON protected by filesystem permissions. Use `--env-only` when credentials should not be saved; every agent must then inherit them. Process environment variables are not an encrypted secret store either.

SQLite retains rendered pending notification messages until sent, dropped, or expired; logical row deletion is not secure erasure of database pages, backups, snapshots, or SSD storage. Diagnostic logs omit message text and credentials. Raw terminal input is forwarded but not recorded. There is no terminal transcript file.

Private backups contain complete preexisting agent settings, which could themselves include unrelated credentials. `uninstall --purge` deletes the utility’s credentials, queue, logs, config, and backups, but does not promise forensic erasure or deletion of external backups. It does not remove unrelated settings.

## Agent safety

Hooks deliberately return no decision. Network delivery occurs in a separate sender, so notification failures are not translated into permission denials or forced continuation. The wrappers never inject answers or add approval/sandbox flags. Codex trust remains under the user’s control.

Treat third-party/custom hook definitions and arbitrary custom regexes as executable configuration. No plugin marketplace downloads, `curl | sh`, cloud relay, inbound webhook server, or automatic software update is installed. Review source changes before rerunning the installer; hook-definition trust alone is not a signature over every imported Python file.

## Reporting

Do not post real tokens, private settings/backups, queue databases, or unredacted agent output in public issues. Reproduce with the bundled synthetic events and fake credentials. Rotate a Pushover token after exposure. Report vulnerabilities privately through this repository’s GitHub Security tab (“Report a vulnerability”) rather than in a public issue.
