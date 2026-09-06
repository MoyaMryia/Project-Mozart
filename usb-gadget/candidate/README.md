# Offline panic-fix candidate

LATEST BATCH: candidate/ now also contains deferred snapshot output and scoped
enable/disable reload-timeout guards. These changes are offline only. See
`BATCH-VALIDATION.md`; use `bash usb-gadget/tests/check-offline.sh` to run the
consolidated checks. Older deployed Build IDs below are historical.

HARDWARE LIMITATION: NVIDIA documents that Tegra Orin/Tegra234 XUDC device
mode does not support isochronous endpoints. Standard UAC1/UAC2 USB audio
therefore cannot work on the onboard UDC. The candidate no longer advertises
`type_iso`; this prevents false enumeration and ring errors. It is not a
standard Windows sound-card implementation. Use an external ISO-capable USB
device controller, or design a custom bulk PCM protocol with a host-side
application.

LATEST: candidate/ now contains the offline DMA-ordering/completion candidate
described in `COMPLETION-FIX.md`. It is NOT the currently installed snapshot
module and has NOT been hardware-tested. Historical Build IDs below refer to
earlier builds; do not deploy by assuming the directory still holds them.

UPDATE 2026-09-06, after the first hardware attempt: the running modules
matched the deployed candidate GNU Build IDs. Windows exposed both audio
devices, but EP7 (USB 0x83 IN) reported ring underrun 0xe 3.451 ms after
re-enable. No panic was observed; this does not prove all lifetime bugs fixed.
The host is now disconnected. The older deployment status below is historical.

The working candidate now contains an OFFLINE-ONLY first-error snapshot build,
not installed in /lib/modules or /boot/initrd. See `ISO-DIAGNOSTICS.md` for its
identity, limitations and the next test gate. The tested modules were preserved
from /lib/modules in `tested-20260906/`, with hashes matching the deployment
record. Do not use the old "running kernel still uses the old modules" statement
below to identify the current runtime: compare GNU Build ID notes instead.

UPDATE 2026-09-06: Candidate modules and initrd are installed on disk, but
not rebooted or hardware-tested yet. The user accepted offline NVMe recovery
instead of a separate recovery boot entry. Do not connect a host yet.
Deployment details and hashes are recorded in
`~/mozart-archive/usb-gadget-backup-20260905/CANDIDATE-DEPLOYMENT-20260906.md`.

The running kernel still uses the old modules. The old boot service is masked.
The sibling top-level .ko files are retained crash-time builds; they do NOT
contain the updated sibling .c sources. Build the candidate only:

```sh
make -C usb-gadget/candidate -j2
python3 -m unittest discover -s usb-gadget/tests -v
bash -n usb-gadget/candidate/mozart-gadget.sh
```

## Changes

- Audio callbacks no longer free requests or feedback buffers. Stop disables
  requeue, disables the endpoint, then frees owned allocations. Failed disable
  retains requests; never-queued and failed-requeue requests remain owned.
- Capture/playback start failures propagate configuration, enable, allocation,
  and queue errors and attempt cleanup. Restart quiesces the prior stream.
- Tegra dequeue completes request-dependent ring updates before giveback and
  returns success after giving back a found request. It no longer dereferences
  a request after its callback or reports EINVAL after completing it.
- UAC2 resets the AC endpoint count when controls are absent, propagates
  interrupt-endpoint setup errors, and does not report alt 1 on failed start.
- Candidate script adds EF/02/01 and MOZART0001, refuses a connected host or
  another gadget's UDC, and checks loaded module Build IDs against candidates.
  It is NOT wired into systemd and does not load/install candidate modules.
- UDC counts unlocked givebacks under its lock. Disable/enable return EBUSY
  while a callback is running; disable-driven callbacks cannot queue requests.
  Pullup-off waits for the IRQ outside the controller lock. The gadget core
  now receives its IRQ number and the async-callback gate, without suppressing
  port events required by the PHY's disconnect completion.
- Complete audio unbind disconnects ALSA and tries all endpoint cleanups.
  Failed teardown retains the parent and callback modules until reboot and
  blocks all subsequent UAC2 binds. Successful teardown waits for ALSA handles
  to close before releasing their backing state.
- Error completions no longer requeue into nuke loops. ISO ring underrun and
  overrun stop the endpoint without trusting an event TRB pointer. Completion
  pointers are range/alignment checked; impossible residual lengths are rejected.

## Validation scope

The three modules compile against the installed 6.8.12-1021-tegra headers.
GCC is 13.3 rather than the kernel header build's 13.2; vermagic matches.

Offline tests extract the actual C cleanup/feedback and dequeue functions,
compile them with mocked USB/DMA operations, and execute with AddressSanitizer,
UndefinedBehaviorSanitizer, and leak detection. They exercise ownership after
disable, synchronous shutdown callbacks, disable failure/retry, unqueued
allocations, failed requeue, partial allocation, and dequeue branches with
callbacks that free or requeue. The retained pre-fix dequeue fails the test.
Data completion ownership also has source-level assertions. There are 15 tests,
including pthread tests holding a callback on another thread while disable is
attempted, nested givebacks, quarantine failures, and 64-bit ring bounds.

These tests do NOT emulate Tegra DMA, validate ISO scheduling, or substitute
for kernel lockdep/KASAN testing. Start allocation/enable failure unwind is
compiled and reviewed, not comprehensively fault-injected on a kernel.

## Test gate

The source-level callback and quarantine blockers above have been addressed.
The recovery prerequisite was explicitly relaxed by the user to offline NVMe
recovery on another Linux computer. The stock backups and RESTORE.md have been
verified; a previous-deployment snapshot was also retained. This fallback
requires physical access, a suitable adapter and an intact root filesystem.

The default initrd path now contains the candidate image; Image and extlinux
configuration are unchanged. Do not hot-unload the running UDC. Reboot only
after confirmation and with the host cable disconnected; verify loaded Build
IDs before any gadget is bound. Candidate startup remains manual.

After those prerequisites, the permitted first test is a single short
enumeration/stream-start attempt with pstore and logging available. Windows
may start streams during discovery, even without pressing Play. Close local
ALSA clients before teardown; snd_card_free can wait for their handles.
Any EBUSY, quarantine, ring error, warning or stall terminates that test:
disconnect the host, preserve evidence, and do not recreate/rebind/unload
modules to recover. Reboot into the recovery entry if necessary.

The underlying ISO ring scheduling error is not proven fixed. Error handling
now stops rather than attempting recovery; this is not clearance for sustained
audio, repeated rebind stress, hot driver removal, or automatic startup.

The deployed candidate initrd SHA-256 is:

    61129730a708c92ebf0d86c2586da5b05447747abe7abd0c618eecd5e5d23530

RESTORE.md in the backup directory has been corrected to restore both initrd
and the UDC module and mask the experiment service. See
../crash-20260905-serial-test/README.md for the panic.
