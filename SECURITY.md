# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in callmem, please report it privately by opening a GitHub Security Advisory at:

https://github.com/DANgerous25/callmem/security/advisories/new

Alternatively, you can email the maintainers directly. We aim to acknowledge reports within 48 hours and provide a fix within 14 days.

## Scope

The following areas are in scope for security reports:

- **Sensitive data vault** — bypass of two-layer detection (pattern + LLM) leading to credential/PII leakage into memory
- **Encryption** — weaknesses in Fernet vault key derivation or storage
- **Network exposure** — web UI or MCP server exposing sensitive data over the network without authentication
- **Code injection** — injection through config files, prompt data, or MCP tool arguments

The following are out of scope:

- **Ollama endpoint security** — securing the local Ollama API is the user's responsibility
- **Operating system security** — file permissions, user isolation
- **Denial of service** — a local tool is inherently vulnerable to local DoS

## Supported Versions

| Version | Supported |
|---------|-----------|
| >= 0.3.0 | Yes |
| < 0.3.0 | No |

## Security Architecture

callmem uses a two-layer sensitive data detection pipeline:

1. **Pattern matching** — regex patterns catch API keys, passwords, tokens, and credit card numbers at ingest time (fast, runs first)
2. **LLM classification** — a local model classifies content for context-sensitive secrets (runs second)

Detected values are encrypted with Fernet symmetric encryption and stored in a vault table. The memory database stores only redacted placeholders (`[REDACTED:vault:<id>]`). See `docs/sensitive-data.md` for full details.
