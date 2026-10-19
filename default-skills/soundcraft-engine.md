---
name: soundcraft-engine
description: How to make and mix audio with the SoundCraft Engine app — sessions, audio, instrument and MIDI tracks, built-in instruments and effects, a tone generator, mixing, and bouncing the mix to WAV, AIFF or FLAC. Present when the person has installed SoundCraft Engine.
present_if_app_installed: soundcraft-engine
allowed-tools: mcp_connections, operate, image_generate, context
---

# SoundCraft Engine

SoundCraft Engine runs SoundCraft, an open-source audio workstation in the
style of Pro Tools, headless in the person's own space, for you. It is an MCP
server named `soundcraft-engine` (skill 'mcp', "An installed app's own
server"): connected when the app was installed, nothing to approve. One
session stays open in the app between calls.

## When to use

Short pieces of music and sound: a synth part and a drum pattern from the
built-in instruments, a tone or noise, the person's own recordings mixed with
EQ, compression, reverb and delay, and the mix bounced to a file.

## First time

1. `mcp_connections(op='tools', name='soundcraft-engine')`: it wakes the app.
   The tools are SoundCraft's own (`new_session`, `open_session`,
   `save_session`, `import_audio`, `list_commands`, `execute`, `batch`,
   `inspect_session`, `bounce_mix`) and the app's `file_put`, `file_get`,
   `file_list`.
2. Write `skills/mcp-soundcraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-soundcraft-engine', content:…})`.

## Steps

1. **A session:** `new_session(name, sample_rate: 48000)`, then
   `execute(command: 'event.tempo', params: {bpm: 120})`.
2. **Tracks:** `execute(command: 'track.new', params: {kind: 'instrument', name: 'Synth', format: 'stereo', instrument: 'subtractive_synth'})`.
   `kind` is audio, aux, midi, instrument, …; the built-in instruments are
   `subtractive_synth` and `drum_synth`. `execute(command: 'engine.plugins')`
   lists every plug-in with its parameter ids and ranges.
3. **Notes:** `execute(command: 'midi.new_clip', params: {track: 'Synth', start: 0, length: {seconds: 4}})`
   answers `{clip}`. Add notes with
   `execute(command: 'midi.note_add', params: {clip, pitch: 60, start_ticks: 0, length_ticks: 900, velocity: 100})`,
   many at once with `batch(calls: [{command, params}, …])`.
   `midi.notes {clip}` lists them back. (The recipe spaced notes 960 ticks
   apart at 120 bpm for quarter notes.)
4. **Audio in:** `file_put(path: 'in/<name>.wav', data: …)`, then
   `import_audio(path: 'in/<name>.wav')`; it answers the new `track` id. WAV,
   AIFF, FLAC, MP3, OGG, AAC/M4A, ALAC and CAF are accepted.
5. **A tone:** put the Signal Generator on an empty audio track:
   `execute(command: 'mix.insert', params: {track: 'Tone', plugin: 'signal_generator', params: {waveform: 0, freq: 220, level: -24}})`
   (waveform 0 Sine, 1 Square, 2 Saw, 3 Triangle, 4 White Noise, 5 Pink Noise).
6. **Effects and mix:** `mix.insert {track, plugin, params}` with `plate_reverb`,
   `room_reverb`, `compressor`, `eq_7band`, `mod_delay`, `chorus`, …;
   `mix.volume {tracks: ['Tone'], db: -12}`, `mix.pan {tracks: ['Synth'], pan: -0.3}`.
   Tracks are named or given by the id a call answered.
7. **Save:** `save_session(path: 'sessions/<name>.scraft')`.
8. **Bounce:** `bounce_mix(path: 'bounces/<name>.wav', format: 'wav', bit_depth: '24', start: 0, end: {seconds: 5})`.
   Formats wav, aiff, flac; bit depth 16, 24 or 32f. Set `end` past the last
   note so reverb tails ring out. Times are samples, `{seconds: x}`, or
   strings like `"0:12.500"`.

## Checks

- `inspect_session` shows tracks, clips, inserts, the mixer and the selection.
- The bounce answers `peak_db`, `true_peak_db` and `lufs` (the recipe's mix:
  peak -10.7 dBFS, -25.4 LUFS); check them against what you expect. A bounce
  does not come back to you as sound or
  an image; it is in the app's window, where the person plays or downloads
  it, and `file_list` shows its size.

## When it fails

- **"select audio clips first"** from `audiosuite.process`: it works on clips;
  for a tone use the Signal Generator insert (step 5).
- **"no track with id …"**: use the id the call answered or the track's name.
- **Long bounces**: a bounce runs at about 100 times real time here and holds
  the whole mix in memory, about 0.66 MB per second of audio (10 minutes
  took 6 s and 400 MB, 30 minutes 1.2 GB, when measured). Past the app's
  memory the program restarts and loses what was not saved: save first, and
  bounce a session longer than that in parts with `start` and `end`.

## Reference

SoundCraft is MIT or Apache-2.0 (github.com/storytold/soundcraft). Its demo
song and instruments are synthesised in code; it bundles no samples.
