# Playwright MCP — local developer checks

Playwright MCP supplements the canonical Python Playwright tests; it does not replace them or become an AOS runtime/model gateway. The local `.codex/config.toml` configures a separate headless browser, not the user's browser or the Docker/noVNC desktop. It cannot grant agent task, approval, takeover, training or deployment authority.

**Separate runtime integration:** the user wants AOS to drive the visible Chromium inside the Ubuntu Agent Computer through Playwright MCP, with actions visible in noVNC. The first opt-in [desktop MCP adapter](DESKTOP_MCP_RUNTIME.md) implements only the fixed offline form through the existing scheduler/policy boundary. General web application onboarding and learning remain planned in the [roadmap](ROADMAP.md); the host developer smoke checks documented here do not satisfy those runtime milestones.

## Local setup

Microsoft's [`@playwright/mcp` 0.0.82 package](https://github.com/microsoft/playwright-mcp/blob/v0.0.82/package.json) requires Node 18+ and pins its Playwright dependencies. This host has Node 24.21.0 and an existing Chromium revision 1243 executable. MCP uses that executable; compatibility must be tested, not inferred from the package version.

The project-local config is **host-specific and excluded from the source package**. It anchors `cwd` to `/home/cachyos/aos` and uses `models/playwright/chromium-1243/chrome-linux64/chrome`. On another checkout, explicitly review those paths and the target origin before copying the local config. Global Codex configuration and application dependency locks are unchanged.

The one-time, version-pinned developer package bootstrap is:

```sh
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm exec --yes --package=@playwright/mcp@0.0.82 -- playwright-mcp --version
```

This populates npm's execution cache, not application dependencies. Subsequent config launches use `npx --offline --no-install @playwright/mcp@0.0.82`; a missing cache fails instead of silently downloading packages. No browser installation is requested.

Inspect just this entry from the repository:

```sh
codex mcp get playwright --json
```

This checks configuration, **not a live tool connection**. Open a new Codex session in this trusted project and check `/mcp`; an already-open session does not gain browser tools from writing a file. Project trust and MCP settings are documented in [Codex MCP configuration](https://developers.openai.com/codex/mcp).

## Scope and private output

The stdio configuration opens no MCP HTTP listener. It requests an isolated profile, blocks service workers and page-registered WebMCP tools, and limits Codex's exposed tools to explicit UI navigation/inspection/interactions. Host-code execution, file upload, browser installation and storage export are not allowlisted. It does not disable Chromium's sandbox or permit unrestricted filesystem access.

The trusted-origin filter targets only `http://127.0.0.1:8765`. **This filter is not a security sandbox and does not cover redirects.** Use only the owned local application; review any external navigation or different fixture port separately. Upstream documents these limitations in the [versioned configuration reference](https://github.com/microsoft/playwright-mcp/blob/v0.0.82/README.md#configuration).

Each launch creates `data/playwright-mcp.XXXXXX` with mode `0700` under `umask 077`. Automatically named screenshots go there. Explicit output filenames resolve against the workspace instead: omit the filename or use this private directory, never a source path. `data/**` is excluded by `.gitignore` and package tooling. Do not collect tokens, use the user's login/session, save storage state, or commit screenshots/traces. Browser isolation is not a container or a comprehensive host/filesystem/network sandbox.

## Read-only smoke

With the local pilot already running, use `browser_navigate` for `http://127.0.0.1:8765/ui/`, inspect `browser_snapshot`, capture an automatically named `browser_take_screenshot`, and inspect `browser_console_messages`. The unauthenticated English/Türkçe language switch may be checked without logging in. Finish with `browser_close` and close the MCP process. Do not start jobs, approve actions or take control for this smoke.

Record the actual initialized server version, returned tool names, page/snapshot result and browser cleanup separately from configuration validation. Authenticated workflows still require disposable fixture sessions and their normal explicit action approvals. Retain private evidence only as needed; remove only the exact per-launch output directory when it is no longer required.

## Verified locally — 2026-09-21

- The explicit cache bootstrap and subsequent offline launch both returned `Version 0.0.82`; the cached npm lock records registry tarball URLs and SHA-512 integrity for MCP and its two pinned Playwright packages.
- An independent JSON-RPC stdio client initialized the configured server, listed 25 upstream tools and confirmed all 15 Codex-allowlisted names exist. The server's initialization version is its underlying Playwright version, `1.64.0-alpha-1789764292000`, not the MCP package version.
- Navigation to `/ui/`, the signed-out English accessibility snapshot and a visually inspected screenshot succeeded with the existing Chromium. Browser console: zero errors and warnings. No login or task/control interaction was performed; the language toggle was not exercised in this smoke.
- Evidence remains under private `data/playwright-mcp.5f7XJO/` (directory `0700`, files `0600`). `browser_close` succeeded and the MCP process exited with code 0.
- TOML parsing and `codex mcp get playwright --json` succeeded. The current Codex conversation still has no dynamically added MCP browser tools; tool availability in a new trusted session remains a separate check.
