# Completion candidate capture

## Result

Windows enumerated and showed both audio devices, but opening Sound Settings
again triggered EP7 ring underrun. This candidate did NOT resolve the observed
failure. The user was instructed to disconnect; unplug confirmation and capture
shutdown remain pending at this update.

Kernel timestamps: enable at 357.616331, re-enable at 357.617701, snapshot at
357.619793. Software enq=2/deq=0/PCS=1 with two queued requests. Event points
to 0xfffdd820 with code 14; context dequeue is the same address with DCS=1.
TRB0 and TRB1 are 192-byte ISO/IOC/SIA descriptors (control 0x80001421), and
TRB2 is empty. Trace shows two successful queue calls after re-enable and no
successful audio giveback before the error. This matches the earlier failure.
No invalid-pointer/residual or disabled-endpoint event diagnostic appeared in
the checked log. No new panic was observed. The snapshot's roughly 0.6-second
printing delay affects post-error timing, not evidence before the first error.

Do not treat DMA barriers or completion validation as a verified underrun fix.
Further investigation must distinguish event production from event consumption
and endpoint restart behavior. No audio playback/recording test is cleared.

Boot verified at 2026-09-06 14:02:33; loaded UDC Build ID is
a8531d9fc643ce034a7af2955c371b8cfb5abd69. All three loaded modules match
candidate/. Service mozart-gadget remains masked.

Capture armed before manual gadget bind at 14:06:25 CST:

- Instance: /sys/kernel/tracing/instances/mozart_completion
- Events: gadget queue, giveback, enable, disable, state; clock mono
- Buffer: 2048 KiB per CPU; tracing_on=1
- Transient services: mozart-completion-trace, mozart-completion-kernel
- Output: usb.trace, kernel-follow.log; baseline and boot ID also saved
- Limits: two hours (approximately 16:06:25 CST), 64 MiB per output file

Host was not attached at the pre-bind gate. After bind, UDC reports default;
trace reports speed 0/disconnected. Only EP0 enabled, no new ISO error or panic.
Request count remains 2; audio settings unchanged. Windows test is pending.
Do not start another trace_pipe reader. Stop the attempt on errors; do not
rebind/unload to retry. Passing enumeration alone does not qualify audio.
