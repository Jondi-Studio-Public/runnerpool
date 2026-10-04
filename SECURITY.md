# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub security advisories: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public issue or pull request for
a security problem. Include what you found, how to reproduce it, and the version or commit.

This is a volunteer project: expect an acknowledgement within a few days, not hours.

## Scope

In scope: the `runner` CLI, the Mac, Windows and Linux installers and on-machine tools, the dashboard,
the watchdog, and the shared workflows and actions in this repository.

Out of scope: misconfiguration of your own fleet, notably attaching self-hosted runners to public
repositories or to workflows that run fork pull requests (see the README's SECURITY section; it is
documented as unsafe), and vulnerabilities in third-party software (GitHub's runner, Tailscale and so on),
which should be reported upstream.

## If you published a credential-baked installer

The installers built by `runner build-local` / `publish` embed a GitHub App key (or token) and a Tailscale
auth key. If one ever left your control, treat the keys as compromised: rotate the GitHub App key (or revoke
the token), revoke the Tailscale auth key, and rebuild.
