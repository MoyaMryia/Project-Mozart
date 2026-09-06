# Armed diagnostic capture

## Completed Attempt

The host was connected once and Windows Sound Settings exposed both devices.
The second ISO IN activation then reported ring underrun. The user unplugged
the host; UDC returned to not attached. Tracing was subsequently disabled and
both collector services stopped after a drain interval; files were synced.
No further bind, module operation or hardware test was performed.

Evidence (kernel journal timestamps):

- 586.678075: EP7 enabled.
- 586.679131: EP7 disabled.
- 586.679146: a transfer event was discarded on disabled EP7; code unknown.
- 586.680266: EP7 enabled again.
- 586.683112: snapshot, software enq=2, deq=0, PCS=1, queued=2.
- Ring DMA base 0xfffdd800; hardware context dequeue 0xfffdd820, DCS=1.
- TRB0/1 each length=192, control=0x80001421 (ISO, IOC, SIA, cycle 1).
- TRB2 is zero/cycle 0; event code=14, pointer=0xfffdd820, endpoint ID=7.
- 657.485261 onward: two ESHUTDOWN givebacks and endpoint disable on unplug.

EP7 is controller index 7, USB ep3in/address 0x83, not a different endpoint.
usb.trace records both requests accepted after the second enable, without a
successful giveback before unplug. This supports hardware advancing to an
unpublished slot without software retiring either TD. It does not establish
successful USB delivery or why completion events were missing. The disabled
event occurred BEFORE the second enable, not as a consequence of its underrun.
Trace and journal clocks show an offset; do not directly subtract timestamps
between the files for latency measurements.

The pre-halt snapshot was copied before its diagnostic output. Printing it
took roughly 0.6 seconds in the IRQ path, so post-error timing is perturbed.
This does not explain the first error, but this logging is unsuitable for
sustained or duplex audio validation. No new panic was observed; absence of a
panic does not prove all lifetime defects fixed.

## Acceptance After Repair

The user requested real audio validation in both directions, then simultaneous
operation. This is pending resolution of the completion/underrun issue.

1. Jetson ALSA playback -> Windows microphone input: send a known, low-level
   test waveform; record on Windows and verify duration, level, frequency and
   continuity, not just device visibility or a moving meter.
2. Windows speaker output -> Jetson ALSA capture: the user plays audio while
   Jetson records it. Check the recording and USB completion/error evidence.
3. Only after both directions pass separately, repeat simultaneously and
   verify stream stop. Keep levels low and disable microphone monitoring to
   avoid feedback. Do not retry streaming with the current failed endpoint.

## Original Preparation

Prepared 2026-09-06 at 13:06:53 CST after user approval, with the host cable
confirmed unplugged. All three loaded module Build IDs matched candidate/.
Manual gadget startup succeeded after tracing was armed. Request count remains
2; no audio settings were changed. No Windows test has been performed yet.

- Trace instance: /sys/kernel/tracing/instances/mozart_iso
- Events: gadget queue, giveback, enable, disable, gadget state
- Clock: mono; buffer: 2048 KiB per CPU; tracing_on: 1
- Collector units: mozart-iso-trace.service, mozart-iso-kernel.service
- Files: usb.trace, kernel-follow.log, kernel-baseline.log, boot-id
- Units are transient, not enabled at boot. Each stops after two hours
  (approximately 15:06:53 CST), or on reaching its 64 MiB output limit.

After bind the UDC reports default, not configured; trace reports speed 0 and
disconnected. Only EP0 enabled in the kernel log. No new ring error or panic.
Do not interpret the post-bind default state as proof of a connected host.

Before connecting Windows, check both collectors are active and tracing_on is
still 1. Windows can start ISO traffic during discovery. Allow only one short
attempt; stop on an error and preserve evidence without rebind/unload/retry.
trace_pipe is consumed by the collector; do not start a second reader.
The ftrace buffer is volatile and ordinary file output is not guaranteed
crash-durable. Pstore remains the fallback for kernel error/snapshot output.
