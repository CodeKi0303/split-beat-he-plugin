# Physical workflows

Use live tool schemas for parameter names and bounds. These steps describe the necessary physical conditions, not permission prompts for already requested settings.

## Batch Hall calibration

1. Identify the requested Hall keys using `keyboard_layout`. Explain that the user must release every key for the baseline, then press each selected key fully. Wait for readiness if their hands are not known to be clear.
2. Start `keyboard_batch_calibration_start`. Its initial three seconds collect the released baseline; afterward the background job records presses. Poll `keyboard_batch_calibration_status` and report which keys have valid captures. Do not interpret idle noise as a completed press or claim an unpressed key is calibrated.
3. The user may press keys in any order. Apply valid requested captures with `keyboard_batch_calibration_apply`; leave uncaptured keys unchanged. Cancel with `keyboard_batch_calibration_cancel` if physical conditions were wrong. Capture is bounded, so inspect the final state if it expires.
4. Verify the returned/read-back endpoints. Save and restore/enable output only to the extent requested. Total stroke remains a separate physical input; two endpoints do not establish the travel curve's physical linearity.

For one-key firmware calibration, `keyboard_calibrate` uses `capture_rest`, `capture_press`, `commit`, and `cancel`. Have the user hold the appropriate pose and inspect status before moving to the next stage. Captures require fresh stable samples; commit applies RAM settings, not Flash persistence.

## Sensors and interference

- `keyboard_capture_sensors` returns bounded host observations and statistics. It can show gross jitter/trends; USB telemetry undersamples the ADC stream.
- `keyboard_capture_key_timing(key, duration_seconds=5.0, sample_hz=100, include_samples=true)` records one Hall key's raw/travel and actual pressed bit without changing output. It returns request/response timestamps, source age, observed press/release brackets and complete hold-duration bounds. Do not treat midpoint estimates as exact firmware event timestamps, bridge stale/reset gaps, or use a capture whose `timing_usable` is false to recommend RT changes.
- For `keyboard_interference_baseline`, ask the user to release all keys and keep the keyboard still. Then have them hold only the selected Hall key for `keyboard_interference_compare`. Examine other keys' deltas; motion, power, sensor noise, and magnet coupling are possible causes. If the baseline is stale or rejected, collect it again in the correct pose.
- `keyboard_measure_performance` / `keyboard_measure_link` use firmware counters and elapsed time. Unsupported firmware/demo results must remain unsupported. Windows USB event tracing or external instruments are separate evidence.

## Rhythm and release practice

Use Split-Beat HE Studio's **리듬 · RT 측정** screen for audible timing references. The MCP timing capture is read-only and does not play audio. Select a calibrated Hall key, BPM and notes per 4/4 bar (8/12/16 etc.); use repeated taps or sustained piano-style holds with a chosen hold/rest length. The chart separates target sound times, corrected targets, travel and observed pressed intervals. Positive target offset means later than the sound; negative means earlier. A personal offset suggested after repeated taps combines player timing, audio and device behavior: it is not isolated keyboard latency. Inspect observed timing intervals and coverage before using any actuation/release-RT candidate; candidates and offsets are never automatic device changes. Physical presses, sound start and any requested setting application remain explicit user actions.

## Firmware update

The bundled plugin supports inspection. Install with [Studio](https://studio.codekilab.com/update.html), or follow the steps below when a separately configured MCP server already has explicit firmware-write permissions.

1. Use `keyboard_firmware_inspect` on the local package. Check manifest, target, hardware revision, and image/hash validation. Check the connected device's reported capabilities separately. Never infer hardware compatibility from a filename alone.
2. The user's request must cover installing this firmware on the named left/right target. If it only asks to inspect, prepare, or enable tools, stop after preparation. Do not silently change server permissions.
3. Preserve a configuration export and any Studio stroke metadata. The tool rejects dirty RAM or active calibration; save/cancel according to the user's intent before updating. It never saves implicitly. A package inspection does not prove that a compatible permanent bootloader is installed; the updater's device preflight is authoritative.
4. With firmware-write permission enabled, call `keyboard_firmware_update` using the inspected package, target, the inspection's **`image_sha256`** as `expected_sha256` (not `package_file_sha256`), `confirm_update=true`, and read-back verification. Keep power/inter-board wiring connected. Do not run another configuration client concurrently.
5. Verify the update result, re-read version/capabilities and configuration, and distinguish image/read-back verification from physical keyboard testing. This app updater does not provide arbitrary Flash writes, a permanent-bootloader upgrade, or automatic rollback. An interrupted update may require reapplying the complete app through recovery mode.
