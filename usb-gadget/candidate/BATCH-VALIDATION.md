# Consolidated validation

## Current Scope

Hardware support finding: NVIDIA documents that Tegra Orin/Tegra234 XUDC
device mode supports control, bulk and interrupt endpoints, but not
isochronous endpoints. Standard UAC1/UAC2 gadget functions require ISO
streaming, so the onboard UDC cannot provide a standards-compliant Windows
USB sound card. The local `type_iso` capability addition has been removed
from the production candidate. This prevents misleading enumeration followed
by ring underrun; it does not add audio functionality.

The user requested fewer hardware cycles, a consolidated test pass, and asked
whether software disconnect could replace physical unplugging. Software
pullup-off/unbind exists, but is not qualified as a safe recovery mechanism on
this board. Earlier connected teardown/hot-unload hung USB and SSH. Do not
leave the host connected for module changes or automatically recover by
unbind/rebind. This batch is offline; no deployment or reboot is requested.

Current candidate adds deferred one-shot snapshot logging and checks the
reload return value in endpoint enable/disable. Disable timeout retains queued
requests and blocks normal endpoint reuse; enable timeout reports failure and
blocks reuse. No successful completion is manufactured from a ring underrun.
The worker uses a preallocated copy, not live DMA pointers. Cleanup blocks
new snapshot publication under the controller lock before cancel_work_sync.

These are scoped repairs, NOT proof of the underrun cause. Other hardware
transition helpers still ignore poll failure, as do other reload callers.
Reset/disconnect and teardown require a broader DMA-quiescence review before
claiming controller-wide timeout safety. The latest deployed completion
candidate failed identically to the earlier snapshot build; see
../completion-test-20260906/. No new hardware cycle is cleared here.

## One Offline Entry Point

```sh
bash usb-gadget/tests/check-offline.sh
```

Builds the candidate, runs all regression tests, checks shell syntax/source
copy consistency, then reports the module identity. It never installs or
operates a gadget. Twenty-one tests passed in this batch. Snapshot and barrier
placement checks include source assertions; they do not model DMA timing.
Enable timeout is currently source-checked; disable timeout is fault-injected
in the extracted C harness, with callbacks and resource counters checked.

## Next Hardware Session

Prepare all recordings, collectors and a low-level known stereo test waveform
before the next authorized deployment. Each stage requires clean logs and
the previous stage passing. Preserve the same configuration across stages.

| Stage | Action | Required evidence |
| --- | --- | --- |
| Boot | Verify loaded Build IDs, service masks, no host | Correct candidate, no new fault |
| Enumeration | Connect once, then open Sound Settings | Configured plus successful ISO completions/requeues, no ring error |
| Windows input | Jetson playback sends known low-level waveform | Windows recording matches frequency, duration, level and continuity |
| Windows output | User plays audio; Jetson records gadget capture | Nonempty correct recording, clean completions |
| Duplex | Run the preceding two directions together briefly | Both recordings continuous, no starvation/errors |
| Stop | Stop application streams with bounded waits | Clean endpoint shutdown, no stuck request or crash |

The Windows side is not currently automated or remotely controlled. The user
must select the correct endpoints and provide the input recording. Disable
microphone monitoring to avoid feedback. Do not describe device visibility or
meter movement alone as passing an audio stage.

On first ring error, warning, timeout, quarantine, stall or panic, abort the
remaining stages and preserve evidence. Never continue tests on failed state.
Software disconnect qualification is a separate later test, not the emergency
stop for this session. Physical disconnect and independent power remain needed.
