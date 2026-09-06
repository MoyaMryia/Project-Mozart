# First-error ISO snapshot

## Status

Post-reboot verification: uptime reports boot at 2026-09-06 13:00:59.
The loaded /sys/module/tegra_xudc/notes/.note.gnu.build-id matches diagnostic
Build ID 4ee2491c072a6e9579476220a544dd3b1e3c5cc4. UDC is not attached,
mozart-gadget.service is masked, and the current kernel log has no new
BUG/Oops/panic or ISO ring error. The configfs usb_gadget directory is not
present at this check; manual gadget startup and hardware testing remain pending.

Deployment update: the user explicitly approved deployment and reboot, and
confirmed the host cable disconnected. The diagnostic UDC and initrd are now
installed and verified on disk. Reboot is the next action; loaded identity
has NOT yet been verified after that reboot.

- Installed initrd SHA-256:
  8872b475b7fd4e895816c583b7a23de589360e6c1448a3e998e7c7e733ebb045
- Backup: /boot/mozart-before-iso-diagnostic-20260906/ contains the prior
  initrd, UDC module, extlinux.conf, pre-reboot kernel log, enumeration trace,
  boot ID and the archive replacement script.
- A direct newc member replacement validated 284 members plus the trailer.
  Only the UDC payload and its filesize field changed; other record bytes
  were preserved. Gzip integrity and installed payload comparisons passed.
- Image and extlinux.conf hashes are unchanged; gadget service remains masked.
- No runtime unbind, unload or hardware test was performed during deployment.

The following build-time status is historical:

Built offline on 2026-09-06, not installed or loaded. No reboot, rebind or
USB test was performed for this diagnostic change. Keep the host disconnected.
The authoritative source is ../tegra-xudc.c; make copies it into candidate/.

Diagnostic tegra-xudc.ko:

```text
Build ID: 4ee2491c072a6e9579476220a544dd3b1e3c5cc4
SHA-256: bdb570b21ed67ed81ea9e1575d855fc7bf8228389d871a924ef14a9dfaa11578
vermagic: 6.8.12-1021-tegra SMP preempt mod_unload modversions aarch64
```

The installed/tested UDC has Build ID
dd98694536ee2ccc6080fed08db73707cc770ffe and SHA-256
9ec4ce777cf8da85dec30b2cca78c901dbb111dbc05c47d326583ec0ebdd462e.
Its copy and the two unchanged audio modules are in tested-20260906/.
/boot/initrd remains SHA-256
61129730a708c92ebf0d86c2586da5b05447747abe7abd0c618eecd5e5d23530.

## Capture

On the first ring underrun/overrun per controller lifetime, under the existing
controller lock, the diagnostic copies the raw event, full 64-slot transfer
ring (including LINK), endpoint context, and these sampled registers:

- MFINDEX, EP_RELOAD, EP_PAUSE, EP_HALT
- EP_STOPPED, EP_THREAD_ACTIVE, EP_STCHG

It counts queued requests and their submitted TRBs, requests the existing
endpoint halt, then prints 85 bounded snapshot records. Further ring errors
still request halt, but do not dump again. The original error message follows
the snapshot. No event data pointer is dereferenced. Software dequeue, request
ownership, queue depth configuration and ISO scheduling fields are unchanged.

The snapshot is not atomic with respect to DMA. Context may also reflect
controller writeback timing rather than its instantaneous internal state.
The software queue and submitted-TRB count do not prove unconsumed hardware
work: successful events may still be pending. Printing after halt can delay
other endpoint/event processing. This is a diagnostic build, not a claim of
zero timing impact or a sustained-audio candidate.

There is no per-packet history, activation generation, timeout propagation,
or automatic recovery in this change. A snapshot alone cannot establish stale
event provenance, callback latency, or the root cause of the first underrun.

## Offline Checks

```sh
make -C usb-gadget/candidate -j2
python3 -m unittest discover -s usb-gadget/tests -v
bash -n usb-gadget/candidate/mozart-gadget.sh
cmp usb-gadget/tegra-xudc.c usb-gadget/candidate/tegra-xudc.c
```

All 16 tests passed, including an extracted-C snapshot test with ASan/UBSan.
It covers pre-halt ring copying, all 64 slots, a nonzero software dequeue,
an invalid event data pointer, nullable descriptor, and repeated-error halting
without repeated reads/output. Existing lifetime and ring guards also pass.
No unused-function or frame-size warnings remain. The build still reports
the known compiler-version difference (GCC 13.3 versus kernel GCC 13.2).
These tests do not emulate Tegra DMA or establish hardware correctness.

## Next Test Gate

1. Review and explicitly approve diagnostic deployment and a reboot separately.
   Retain the existing NVMe recovery route and stock backups. Do not hot-unload
   the UDC, rebuild the current gadget, or alter the startup service to recover.
2. With the host disconnected, deploy the diagnostic UDC using the documented
   surgical initrd procedure; preserve the current installed module and initrd.
   Verify both payloads, then reboot only with confirmation.
3. Verify the loaded GNU Build ID note against this diagnostic module. Reading
   /sys/module/.../sections/.text gives an address, not executable bytes, and
   its hash must never be compared with a .ko hash.
4. Verify the old service is masked and no gadget is bound. Verify the two
   audio-module Build IDs before the guarded manual start.
5. Preserve existing trace/log evidence. Prepare pstore and continuous buffered
   gadget enable/disable/queue/giveback tracing BEFORE connecting Windows.
   Avoid per-packet console debug logs. Keep request count and audio settings
   unchanged; Windows may open streams during discovery.
6. Permit one short enumeration/stream-start attempt. Any ring error, timeout,
   warning, quarantine or stall ends the test: disconnect and preserve logs
   plus the complete trace. Do not rebind or unload to retry.

Compare the snapshot's ring DMA base, context dequeue/DCS and slot cycles with
the queued/giveback timeline. First determine whether hardware encountered an
empty ring or apparently published work it did not consume. Do not infer that
increasing request count or changing FrameID is the fix without that evidence.
