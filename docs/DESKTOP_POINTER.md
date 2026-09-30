# Live desktop pointer position

The authenticated `GET /api/desktop/pointer` reads the current X11 pointer position from the **owned Ubuntu desktop container**. A new backend advertises support with `pointer_tracking: true` in `/api/state.runtime`; older running backends omit it. The endpoint accepts no query parameters or client-selected display/container.

When available, the response has `available: true`, integer `x`, `y`, `width`, `height`, and `sampled_at`. Coordinates are pixels relative to the X11 display, not the browser viewport. When stopped, unsupported, stale, or temporarily unreadable, it returns `available: false`, a bounded reason, and `sampled_at`; it never returns old coordinates as current.

The backend verifies the exact container ID, runtime label, pinned image and source label before one fixed, time-bounded `xdotool` read. It rechecks the session generation and container identity after sampling. The endpoint does not move/click the pointer, capture screenshots, read page content, write trajectory data, or expand task authorization. Polling is UI display only, not evidence of an action or a replacement for independent outcome verification. The existing managed server must be restarted explicitly before it can advertise this new endpoint; source/UI changes alone do not update a running backend.
