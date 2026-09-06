# DMA ordering and completion candidate

## Deployment Update

Post-reboot update: boot time 2026-09-06 14:02:33, loaded UDC Build ID verified
as a8531d9fc643ce034a7af2955c371b8cfb5abd69. Audio modules also matched.
Continuous tracing was armed before manual gadget bind at 14:06:25 CST.
See ../completion-test-20260906/ for the new capture. Hardware audio validation
remains pending; no ISO endpoint has started as of this readiness check.

2026-09-06: user approved deployment and explicitly reserved reboot for manual
execution. The candidate UDC and matching initrd are now installed and verified
on disk. No reboot, runtime module unload or gadget rebind was performed.
Post-reboot loaded Build ID verification and hardware testing remain pending.

- Installed initrd SHA-256:
  5d33664f38e630b62dc0b47b477e22abfa2a8c931004749b57db2beb8119fbdf
- Installed UDC SHA-256:
  f86d8911df1de02b7fcd30eb7d76fdbea0b012c6c3cc0829ce7012e56fddcbd9
- Expected loaded Build ID after reboot:
  a8531d9fc643ce034a7af2955c371b8cfb5abd69
- Previous diagnostic initrd/module, extlinux.conf, boot ID, kernel log and
  archive replacement script: /boot/mozart-before-completion-fix-20260906/
- All 284 archive members plus trailer validated; only UDC payload and its
  filesize field changed. Gzip integrity and installed-file comparisons pass.
- Image and extlinux.conf hashes unchanged. Service remains masked. UDC was
  not attached at the deployment gate. Keep the host cable disconnected.

The following describes the earlier offline build stage:

Built offline 2026-09-06 after the captured EP7 underrun. NOT installed,
loaded, or hardware-tested. The installed snapshot module and initrd remain
unchanged. Host is disconnected; the prior capture services are stopped.

## Changes

- Add dma_wmb before publishing transfer-TRB cycle ownership. The existing
  writel doorbell ordering remains, but body-before-cycle ordering now also
  covers hardware already consuming the ring.
- Read event ownership with READ_ONCE and execute dma_rmb before consuming
  the event body. These are DMA ordering fixes, not proof of the underrun cause.
- Move success/short completion retirement into the completion function.
  Reject invalid pointers, LINK completions and impossible matched-request
  residuals before modifying software dequeue or ring_full.
- Preserve retirement of pending events for requests already dequeued. An
  earlier draft rejected every unmatched event; review showed that could
  strand ring capacity after cancellation, so that draft was removed.
- Include raw completion code and event words in rate-limited diagnostics for
  events arriving on a disabled endpoint. Previously that evidence was lost.

Underrun still fails closed; it is not converted into success. No request-depth,
ISP, FrameID, interval, alternate-setting or automatic recovery changes.

## Verification

make -C usb-gadget/candidate -j2 passed, with only the known GCC 13.3 versus
kernel GCC 13.2 warning. All 21 unittest tests passed. The new extracted-C
completion test runs with ASan/UBSan and checks invalid pointer/LINK/residual,
cancelled-request retirement, normal/short/wrapped/partial completion and a
callback that frees its request. DMA tests check source ordering only; they
do not emulate weakly ordered DMA or prove hardware correctness.

```text
tegra-xudc.ko Build ID: a8531d9fc643ce034a7af2955c371b8cfb5abd69
SHA-256: f86d8911df1de02b7fcd30eb7d76fdbea0b012c6c3cc0829ce7012e56fddcbd9
```

## Remaining Work

The cause of absent successful ISO completions remains unproven. In particular,
ring address reuse can still make an old event match a new request, and endpoint
transition poll failures are not propagated. This patch does not claim either
problem solved. No speculative IRQ drain or ISO recovery algorithm was added.

The existing full snapshot prints in IRQ context and measurably delays
post-error processing; it remains diagnostic-only, unsuitable for sustained
duplex qualification. Before another authorized short hardware test, preserve
the installed snapshot module, use the existing deployment/recovery gate,
and arm continuous tracing before connection. A new deployment and reboot
have not been performed for this candidate.

Only after error-free completion/requeue behavior is verified should the
requested known-signal Windows microphone test, Windows playback-to-Jetson
recording test, and finally simultaneous operation proceed.
