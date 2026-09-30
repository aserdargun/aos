# Security Policy

## Project status

AOS is experimental software, not a hardened general-purpose computer agent. Its container shares the host kernel and is not equivalent to a VM. The supported workflows are bounded and explicitly authorized; arbitrary websites, intranet systems, accounts, or company applications are not generally supported.

## Reporting a vulnerability

This repository does not currently publish a dedicated security contact or private advisory workflow. Do not post exploit details, credentials, private trajectories, or sensitive reproduction data in a public issue. Contact the repository owner through a trusted private channel and agree on a safe disclosure process before sending sensitive artifacts. Do not assume a response SLA.

## Data and credentials

Do not include tokens, passwords, cookies, authorization headers, private keys, internal URLs, real user data, screenshots, trajectories, local databases, or model/adapter weights in issues or patches. Share the smallest sanitized reproduction possible. Local databases are not automatically encrypted; protect sensitive host storage with appropriate operating-system controls.

## Security invariants

- Model output and page content do not create authority. A valid human approval is scoped to the exact action, state, runtime, and lease.
- Host and agent workspaces are separate. Do not add host shell or broad filesystem access to work around a runtime limitation.
- Network access is explicit and scope-bound. Do not disable private-IP/SSRF, TLS hostname, origin, redirect, or cookie protections for intranet compatibility.
- Deployment identities and artifacts remain pinned and verified; training and promotion remain explicit.
- Errors fail closed, and owned processes, listeners, locks, and files are cleaned up on cancellation and failure.

An eventual authorized intranet connector must have a separate threat model and acceptance plan. SWAPP is deferred. Do not use internal hosts, accounts, credentials, or data in tests before authorization is provided.
