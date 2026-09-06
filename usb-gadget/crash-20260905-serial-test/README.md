# Serial-number test: kernel panic

Captured on 2026-09-05 after the Windows enumeration test. The three
ramoops files are unmodified copies from /var/lib/systemd/pstore.
These are persistent kernel logs, not a full-memory vmcore.

## Test context

- Runtime-only IAD class change: 00/00/00 -> EF/02/01. Code 10 persisted.
- Next runtime-only change: empty serial -> MOZART0001; other settings kept.
- Both changes used an unbind/rebind with the host disconnected, without
  unloading modules or modifying the boot script, initrd, or boot menu.
- The user observed a USB audio device briefly, followed by a Jetson reboot.
- Prior boot EP0 bind timestamps (9.676181, 2416.209240, 3332.549969)
  match these logs, corroborating their association with this test.
- Wall-clock timestamps jump during boot; use kernel monotonic times below.

## Confirmed crash sequence

In dmesg-ramoops-1:

- 3374.112944: interrupt endpoint enabled.
- 3374.963385 onward: ISO endpoints enabled/disabled repeatedly.
- 3375.136936: completion error 0xe on EP 5 (ring underrun).
- 3375.138936: completion error 0xf on EP 2 (ring overrun).
- 3375.140918: kernel BUG at mm/slub.c:553.
- 3375.517170: Kernel panic, fatal exception in interrupt.

The ring error names do not establish a C buffer overrun.

Relevant stack, outer caller to failure:

    tegra_xudc_irq
    tegra_xudc_handle_ep0_setup_packet
    composite_setup
    afunc_set_alt
    u_audio_stop_capture
    usb_ep_disable
    __tegra_xudc_ep_disable
    tegra_xudc_req_done
    usb_gadget_giveback_request
    u_audio_iso_fback_complete+0xe4/0x100
    kfree+0x25c/0x2f4

Local u_audio.ko disassembly identifies the call before LR +0xe4 as
kfree(req->buf), u_audio.c:290. Archived kernel slub.c:553 is the
freelist-hardening BUG_ON(object == fp), detecting double free or corruption.

## Strongly supported ownership hypothesis

1. u_audio.c:480 dequeues feedback while fb_ep_enabled remains true.
2. tegra-xudc.c:1499 has a path that completes the request with ECONNRESET
   but then returns EINVAL.
3. The completion callback can requeue because feedback is still enabled.
4. u_audio.c:482 treats the dequeue error as a reason to free the buffer
   and request, leaving the requeued request dangling.
5. Endpoint disable gives it back again; the callback frees its buffer,
   matching the observed BUG site.

The dump does not record the first free or dequeue return, so this exact
history is not proven. tegra-xudc.c:1469 also accesses a request after
giveback; changing only the audio enabled flag is not a complete fix.

## Interpretation and safety

Enumeration progressed beyond the earlier Code 10 into stream-interface
operations. A nonempty serial, or its changed Windows device identity,
is implicated in that progress; this does not prove audio streaming works.

The panic is a runtime USB audio/UDC failure, not an initrd unpack failure.
The subsequent boot succeeded; current initrd SHA-256 remains:

    536f00d9f075e234844de529073707b5696c9a8a851070ecae59313ebbb0b2ac

On reboot, mozart-gadget.service ran again and restored 00/00/00 with an
empty serial. Runtime tracing did not survive the reboot. Journald lists
only the current boot. Current kernel.panic=0, kernel.panic_on_oops=1;
systemd reports a 2-minute Tegra hardware watchdog. A watchdog reset after
panic is plausible, but the saved panic log does not prove the reset source.

Do not repeat streaming tests or hot-unload the UDC to reproduce this.
No driver changes, boot-chain edits, or service changes were made during
this crash investigation. Keep the host data cable disconnected pending
an offline ownership/error-path review and an explicit recovery plan.
